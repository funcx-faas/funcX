#!/usr/bin/env python3

import logging
import socket
import argparse
import pickle


def server(port=0, host='', debug=False, datasize=102400):

    from funcx.serialize import FuncXSerializer
    fxs = FuncXSerializer()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, port))
        bound_port = s.getsockname()[1]
        print(f"BINDING TO:{bound_port}", flush=True)
        s.listen(1)
        conn, addr = s.accept()  # we only expect one incoming connection here.
        with conn:
            print('Connected by', addr)
            while True:

                b_msg = conn.recv(datasize)
                if not b_msg:
                    print("Exiting")
                    return

                msg = pickle.loads(b_msg)

                if msg == 'PING':
                    ret_value = (('PING', None))
                else:
                    try:
                        method = fxs.deserialize(msg)
                        print("Deserialization result: ", method)
                        del method
                    except Exception as e:
                        ret_value = (('DESERIALIZE_FAIL', str(e)))

                    else:
                        ret_value = (('SUCCESS', None))

                ret_buf = pickle.dumps(ret_value)
                conn.sendall(ret_buf)


class OffProcessClient(object):

    def __init__(self, port, host='', debug=False, datasize=102400):

        self.port = port
        self.host = host
        self.debug = debug
        self.datasize = datasize
        self.socket = None

    def connect(self):
        if self.socket:
            raise Exception("Socket already exists")

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((self.host, self.port))

    def send_recv(self, buf):
        self.socket.sendall(pickle.dumps(buf))
        ret_buf = self.socket.recv(self.datasize)
        ret = pickle.loads(ret_buf)
        return ret

    def close(self):
        if self.socket:
            self.socket.close()


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url", default='')
    parser.add_argument("-p", "--port", default='0',
                        help="Port over which the client can reach the server. Default= auto-pick")
    parser.add_argument("-d", "--debug", action='store_true',
                        help="Count of apps to launch")
    parser.add_argument("-c", "--client", action='store_true',
                        help="Launch the client instead of the server for testing")
    args = parser.parse_args()

    if args.client:
        # For test/debug only
        client = OffProcessClient(int(args.port), host=args.url, debug=args.debug)
        client.connect()
        for i in range(100):
            x = client.send_recv('PING')

    else:
        server(int(args.port), host=args.url, debug=args.debug)
