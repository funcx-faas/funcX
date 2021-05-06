import codecs
import dill
import pickle
import inspect
import logging

logger = logging.getLogger(__name__)

from funcx.serialize.base import fxPicker_enforcer, fxPicker_shared


class pickle_base64(fxPicker_shared):

    _identifier = '00\n'
    _for_code = False

    def __init__(self):
        super().__init__()

    def serialize(self, data):
        x = codecs.encode(pickle.dumps(data), 'base64').decode()
        return self.identifier + x

    def deserialize(self, payload):
        chomped = self.chomp(payload)
        data = pickle.loads(codecs.decode(chomped.encode(), 'base64'))
        return data


class code_dill_source(fxPicker_shared):

    _identifier = '04\n'
    _for_code = True

    def __init__(self):
        super().__init__()

    def serialize(self, data):
        name = data.__name__
        body = dill.source.getsource(data)
        x = codecs.encode(pickle.dumps((name, body)), 'base64').decode()
        return self.identifier + x

    def deserialize(self, payload):
        chomped = self.chomp(payload)
        name, body = pickle.loads(codecs.decode(chomped.encode(), 'base64'))
        exec(body)
        return locals()[name]


class code_text_inspect(fxPicker_shared):
    """ We use dill to get the source code out of the function object
    and then exec the function body to load it in. The function object
    is then returned by name.
    """

    _identifier = '03\n'
    _for_code = True

    def __init__(self):
        super().__init__()

    def serialize(self, data):
        name = data.__name__
        body = inspect.getsource(data)
        x = codecs.encode(pickle.dumps((name, body)), 'base64').decode()
        return self.identifier + x

    def deserialize(self, payload):
        chomped = self.chomp(payload)
        name, body = pickle.loads(codecs.decode(chomped.encode(), 'base64'))
        exec(body)
        return locals()[name]


class code_dill(fxPicker_shared):
    """ We use dill to serialize the function object
    """

    _identifier = '01\n'
    _for_code = True

    def __init__(self):
        super().__init__()

    def serialize(self, data):
        x = codecs.encode(dill.dumps(data), 'base64').decode()
        return self.identifier + x

    def deserialize(self, payload):
        chomped = self.chomp(payload)
        function = dill.loads(codecs.decode(chomped.encode(), 'base64'))
        return function


class code_pickle(fxPicker_shared):

    _identifier = '02\n'
    _for_code = True

    def __init__(self):
        super().__init__()

    def serialize(self, data):
        x = codecs.encode(pickle.dumps(data), 'base64').decode()
        return self.identifier + x

    def deserialize(self, payload):
        chomped = self.chomp(payload)
        data = pickle.loads(codecs.decode(chomped.encode(), 'base64'))
        return data


def bar(x, y={'a': 3}):
    return x * y['a']


if __name__ == '__main__':

    bar(29)
    # print(pickle_base64.identifier)
    ct = code_text_inspect()
    f = ct.serialize(bar)
    # print("Serialized : ", f)
    new_bar = ct.deserialize(f)
    print("After deserialization : ", new_bar)
    print("FN() : ", new_bar(10))
