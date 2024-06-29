from __future__ import annotations

import logging
import os
import queue
import random
import threading
import time
import typing as t

import pika
from globus_compute_endpoint.endpoint.utils import _redact_url_creds

if t.TYPE_CHECKING:
    from pika.channel import Channel
    from pika.frame import Method
    from pika.spec import Basic, BasicProperties

log = logging.getLogger(__name__)


class TaskQueueSubscriber(threading.Thread):
    def __init__(
        self,
        *,
        queue_info: dict,
        pending_task_queue: queue.Queue,
        poll_period_s: float = 0.5,
        connect_attempt_limit: int = 7200,
        channel_close_window_s: int = 10,
        channel_close_window_limit: int = 3,
        thread_name: str | None = None,
    ):
        """

        Parameters
        ----------
        queue_info: dict
            Dictionary that includes the key "connection_url", as well as
            exchange and queue declaration information specified by the
            server.

        pending_task_queue: queue.Queue
            Messages from upstream will be placed in this queue. Consumers of
            this queue must call .to_ack() with the message id when finished
            processing

        poll_period_s: float
            How often to perform housekeeping tasks (ACKing messages upstream,
            checking stop_event, etc.)

        connect_attempt_limit: int
            Number of connection attempts to fail before giving up.  The
            connection counter will reset to 0 after the connection is
            sustained for 60s, so transient network errors should not build
            up to a future failure.)

        channel_close_window_s: int
            Window of time to count channel close events

        channel_close_window_limit: int
            Limit of channel close events (within ``channel_close_window_s``)
            before shutting down the thread.

        thread_name: str | None
            Name the backing thread; per Python's implementation, this name has
            no semantics.
            default: implementation generated value.
        """

        super().__init__()

        self.queue_info = queue_info
        self.pending_task_queue = pending_task_queue
        self._to_ack: queue.SimpleQueue[int] = queue.SimpleQueue()
        self._stop_event = threading.Event()
        self._channel_closed = threading.Event()

        self._connection: pika.SelectConnection | None = None
        self._channel: Channel | None = None
        self._consumer_tag: str | None = None

        # how many times to attempt connection before giving up and shutting
        # down the thread
        self.connect_attempt_limit = connect_attempt_limit
        self._connection_tries = 0  # count of connection events; reset on success

        # invalid until set in start_consuming
        self._connected_at: int | None = None

        # list of times that channel was last closed
        self._channel_closes: list[float] = []

        # how long a time frame to keep previous channel close times
        self.channel_close_window_s = channel_close_window_s

        # how many times allowed to retry opening a channel in the above time
        # window before giving up and shutting down the thread
        self.channel_close_window_limit = channel_close_window_limit

        self._poll_period_s = poll_period_s
        if thread_name:
            self.name = thread_name

        log.debug("Init done")

    def run(self):
        log.debug("%s AMQP thread begins", self)
        idle_for_s = 0.0
        while (
            not self._stop_event.is_set()
            and self._connection_tries < self.connect_attempt_limit
        ):
            if self._connection or self._connection_tries:
                idle_for_s = random.uniform(0.5, 10)
                msg = f"%s AMQP reconnecting in {idle_for_s:.1f}s."
                log.debug(msg, self)
                if self._connection_tries == self.connect_attempt_limit - 1:
                    log.warning(f"{msg}  (final attempt)", self)

            if self._stop_event.wait(idle_for_s):
                break

            self._connection_tries += 1
            try:
                log.debug(
                    "%r Opening connection to AMQP service.  Attempt: %s (of %s)",
                    self,
                    self._connection_tries,
                    self.connect_attempt_limit,
                )
                if not log.isEnabledFor(logging.DEBUG):
                    if self._connection_tries == 1:
                        log.info(f"{self!r} Opening connection to AMQP service.")
                    elif self._connection_tries == 2:
                        log.info(
                            f"{self!r} Opening connection to AMQP service (second"
                            " attempt).  Will continue for up to"
                            f" {self.connect_attempt_limit} attempts.  To log all"
                            f" attempts, use `--debug`."
                        )
                self._connection = self._connect()
                self._event_watcher()
                self._connection.ioloop.start()
            except Exception:
                log.exception("%s Unhandled exception: shutting down connection.", self)
        self._stop_event.set()
        log.debug("%s Shutdown complete", self)

    def ack(self, msg_tag: int):
        self._to_ack.put(msg_tag)

    def stop(self) -> None:
        log.info("Stopping thread")
        self._stop_event.set()

    def _connect(self) -> pika.SelectConnection:
        pika_params = pika.URLParameters(self.queue_info["connection_url"])
        return pika.SelectConnection(
            pika_params,
            on_close_callback=self._on_connection_closed,
            on_open_error_callback=self._on_open_failed,
            on_open_callback=self._on_connection_open,
        )

    def _on_open_failed(self, mq_conn: pika.BaseConnection, exc: str | Exception):
        count = f"[attempt {self._connection_tries} (of {self.connect_attempt_limit})]"
        if isinstance(exc, pika.exceptions.ProbableAuthenticationError):
            count = "[invalid credentials; unrecoverable]"
            self._connection_tries = self.connect_attempt_limit

        pid = f"(pid: {os.getpid()})"
        exc_text = f"Failed to open connection - ({exc.__class__.__name__}) {exc}"
        msg = f"{count} {pid} {exc_text}"
        log.debug("%r %s", self, msg)
        if self._connection_tries == 1:
            log.warning(f"{self!r} {msg}")

        if not (self._connection_tries < self.connect_attempt_limit):
            self._stop_event.set()
            log.error(f"{self!r} {msg}")
        mq_conn.ioloop.stop()

    def _on_connection_closed(self, mq_conn: pika.BaseConnection, exc: Exception):
        msg_fmt = "%r Connection closed: %s"
        log.debug(msg_fmt, self, exc)
        if self._connection_tries == 1:
            # if 1, then we've not been stable for more than 60s (see _event_watcher)
            log.info(msg_fmt, self, exc)
            log.warning(f"{self!r} Unable to sustain connection; retrying ...")

    def _on_connection_open(self, _mq_conn: pika.BaseConnection):
        log.debug("%r Connection established; creating channel", self)
        self._open_channel()

    def _open_channel(self):
        if self._connection and self._connection.is_open:
            self._connection.channel(on_open_callback=self._on_channel_open)

    def _on_channel_open(self, mq_chan: Channel):
        self._channel = mq_chan

        mq_chan.add_on_close_callback(self._on_channel_closed)
        mq_chan.add_on_cancel_callback(self._on_consumer_cancelled)

        log.debug(
            "%r Channel %s opened (%s)",
            self,
            mq_chan.channel_number,
            mq_chan.connection.params,
        )
        self._start_consuming()

    def _on_channel_closed(self, mq_chan: Channel, exc: Exception):
        self._consumer_tag = None
        now = time.monotonic()
        then = now - self.channel_close_window_s
        self._channel_closes = [cc for cc in self._channel_closes if cc > then]
        self._channel_closes.append(now)
        if len(self._channel_closes) < self.channel_close_window_limit:
            if self._stop_event.is_set():
                return
            msg = f"{self} Channel closed  [{mq_chan}\n  ({exc})]"
            log.debug(msg, exc_info=exc)
            log.warning(msg)
            mq_chan.connection.ioloop.call_later(1, self._open_channel)

        else:
            log.error(
                f"{self} Unable to sustain channel after {len(self._channel_closes)}"
                f" attempts in {self.channel_close_window_limit} seconds. ({exc})"
            )
            self._stop_event.set()

    def _on_consumer_cancelled(self, frame: Method[Basic.CancelOk]):
        log.debug("%s Consumer cancelled remotely, shutting down: %r", self, frame)
        if self._channel:
            self._channel.close()

    def _start_consuming(self):
        try:
            self._consumer_tag = self._channel.basic_consume(
                queue=self.queue_info["queue"],
                on_message_callback=self._on_message,
                exclusive=True,
            )
            self._connected_at = time.time()
        except Exception as e:
            log.warning(
                f"{self} Unable to start consuming messages:"
                f" ({e.__class__.__name__}) {e}"
            )
            self._stop_ioloop()
        else:
            qname = self.queue_info["queue"]
            log.info(f"{self!r} Awaiting messages from queue: {qname}")

    def _on_cancelok(self, _frame: Method[Basic.CancelOk]):
        self._close_channel()

    def _close_channel(self):
        log.debug("%s Closing the channel", self)
        self._channel.close()

    def _stop_ioloop(self):
        """
        Gracefully stop the ioloop.

        In an effort play nice with upstream, attempt to follow the AMQP protocol
        by closing the channel and connections gracefully.  This method will
        rearm itself while the connection is still open, continually working
        toward eventually and gracefully stopping the connection, before finally
        stopping the ioloop.
        """
        if self._connection:
            self._connection.ioloop.call_later(0.1, self._stop_ioloop)
            if self._connection.is_open:
                if self._channel:
                    if self._channel.is_open:
                        self._channel.close()
                    elif self._channel.is_closed:
                        self._channel = None
                else:
                    self._connection.close()
            elif self._connection.is_closed:
                self._connection.ioloop.stop()
                self._connection = None

    def _on_exchange_declareok(self, _frame: Method):
        """
        Invoked by pika when RabbitMQ has finished the Exchange.Declare RPC command.
        """
        log.info(
            "Exchange declared successfully.  Ensuring queue exists:"
            f" {self.queue_info['queue']}"
        )
        assert self._channel is not None
        self._channel.queue_declare(
            passive=True,  # *we* don't create the queue, just consume it
            callback=self._on_queue_declareok,
            queue=self.queue_info["queue"],
        )

    def _on_queue_declareok(self, _frame: Method):
        self._start_consuming()

    def _on_message(
        self,
        mq_chan: Channel,
        basic_deliver: Basic.Deliver,
        properties: BasicProperties,
        body: bytes,
    ):
        try:
            d_tag = basic_deliver.delivery_tag
        except Exception as e:
            log.debug(
                "Invalid Basic.Deliver; unable to process message.  (%s) %s",
                e.__class__.__name__,
                e,
            )
            return

        try:
            log.debug(
                "%s Received message from %s: %s, %s",
                self,
                d_tag,
                properties.app_id,
                _redact_url_creds(body),
            )
            headers = properties.headers if properties.headers else {}
            self.pending_task_queue.put((d_tag, headers, body))
        except Exception:
            # No sense in waiting for the RMQ default 30m timeout; let it know
            # *now* that this message failed.
            log.exception("%s External queue put failed", self)
            mq_chan.basic_nack(d_tag, requeue=True)

    def _event_watcher(self):
        """Polls the stop_event periodically to trigger a shutdown"""
        if self._stop_event.is_set():
            log.debug("%r Shutting down per stop event", self)
            self._stop_ioloop()
            return

        if self._connection_tries and self._consumer_tag and self._connected_at:
            # we're connected ...
            if time.time() - self._connected_at > 60:
                # ... and connection stable for 60s; good to reset connection tries
                self._connection_tries = 0
                log.debug(
                    "%r Connection deemed stable; resetting connection tally", self
                )

        delivery_tags = []
        try:
            while True:
                delivery_tags.append(self._to_ack.get(block=False))
        except queue.Empty:
            pass
        if delivery_tags:
            delivery_tags.sort()  # nominally a no-op
            latest_msg_id = delivery_tags[-1]
            self._channel.basic_ack(latest_msg_id, multiple=True)
            log.debug("%r Acknowledged through message: %s", self, latest_msg_id)

        self._connection.ioloop.call_later(self._poll_period_s, self._event_watcher)
