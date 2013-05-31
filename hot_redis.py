
from contextlib import contextmanager
from operator import iand, ior, isub
from uuid import uuid4
from redis import Redis
from redis.exceptions import ResponseError


redis = Redis()
_pipeline = None
client = lambda: _pipeline or redis

@contextmanager
def pipeline():
    global _pipeline
    _pipeline = redis.pipeline()
    yield
    _pipeline.execute()
    _pipeline = None

lua_funcs = {}
with open("atoms.lua", "r") as f:
    for func in f.read().strip().split("function "):
        if not func:
            continue
        name, code = func.split("\n", 1)
        name = name.split("(")[0].strip()
        code = code.rsplit("end", 1)[0].strip()
        lua_funcs[name] = redis.register_script(code)


class Base(object):

    def __init__(self, value=None, key=None):
        self.key = key or str(uuid4())

    def __del__(self):
        self.delete()

    def _proxy(self, name):
        try:
            func = getattr(client(), name)
        except AttributeError:
            pass
        else:
            return lambda *a, **k: func(self.key, *a, **k)
        try:
            func = lua_funcs[name]
        except KeyError:
            pass
        else:
            return lambda *a, **k: func(keys=[self.key], args=a, **k)
        raise AttributeError(name)

    def __getattr__(self, name):
        return self._proxy(name)

    def __repr__(self):
        return "%s(%s, '%s')" % (self.__class__.__name__, self.value, self.key)


class Iterable(Base):

    def __init__(self, value=None, key=None):
        super(Iterable, self).__init__(value, key)
        self.type = None

    def _proxy(self, name):
        func = super(Iterable, self)._proxy(name)
        def wrapper(*args, **kwargs):
            value = func(*args, **kwargs)
            if value is not None:
                return self._set_type(value)
        return wrapper

    def _to_value(self, value):
        if type(value) == type(self):
            return value.value
        return value

    def _is_many(self, value):
        return type(value).__name__ == type(self).__name__.lower()

    def _set_type(self, value):
        if self.type == str:
            return value
        elif self._is_many(value):
            typed_value = map(self._set_type, value)
            if type(typed_value) != type(value):
                # Everything other than lists.
                typed_value = type(value)(typed_value)
            return typed_value
        elif self.type:
            return self.type(value)
        return value

    def _check_type(self, value):
        if self._is_many(value):
            map(self._check_type, value)
        else:
            t = type(value)
            if not self.type:
                self.type = t
            elif t != self.type:
                raise TypeError("%s != %s" % (t, self.type))

    def __eq__(self, value):
        return self.value == self._to_value(value)

    def __iter__(self):
        return iter(self.value)

    def __lt__(self, value):
        return self.value < self._to_value(value)

    def __le__(self, value):
        return self.value <= self._to_value(value)

    def __gt__(self, value):
        return self.value > self._to_value(value)

    def __ge__(self, value):
        return self.value >= self._to_value(value)

class List(Iterable):

    def __init__(self, value=None, key=None):
        super(List, self).__init__(value, key)
        if not isinstance(value, list):
            try:
                list(value)
            except TypeError:
                value = None
        if value:
            self.extend(value)

    @property
    def value(self):
        return self[:]

    def __add__(self, l):
        return List(self.value + self._to_value(l))

    def __iadd__(self, l):
        self.extend(self._to_value(l))
        return self

    def __mul__(self, i):
        return List(self.value * i)

    def __imul__(self, i):
        self.list_multiply(i)
        return self

    def __len__(self):
        return self.llen()

    def __getitem__(self, i):
        if isinstance(i, slice):
            start = i.start if i.start is not None else 0
            stop = i.stop if i.stop is not None else 0
            return self.lrange(start, stop - 1)
        item = self.lindex(i)
        if item is None:
            raise IndexError
        return item

    def __setitem__(self, i, value):
        self._check_type(value)
        try:
            self.lset(i, value)
        except ResponseError:
            raise IndexError

    def __delitem__(self, i):
        self.pop(i)

    def extend(self, l):
        self._check_type(l)
        self.rpush(*l)

    def append(self, value):
        self.extend([value])

    def insert(self, i, value):
        self._check_type(value)
        self.list_insert(i, value)

    def pop(self, i=-1):
        if i == -1:
            return self.rpop()
        elif i == 0:
            return self.lpop()
        else:
            return self.list_pop(i)

    def reverse(self):
        self.list_reverse()

    def index(self, value):
        return self.value.index(value)

    def count(self, value):
        return self.value.count(value)

    def sort(self, reverse=False):
        self._proxy("sort")(desc=reverse, store=self.key)


class Set(Iterable):

    def __init__(self, value=None, key=None):
        super(Set, self).__init__(value, key)
        self.type = None
        try:
            iter(value)
        except TypeError:
            value = None
        if value:
            self.update(value)

    @property
    def value(self):
        return self.smembers()

    def _all_redis(self, values):
        return all([isinstance(value, Set) for value in values])

    def _reduce(self, op, values):
        for i, value in enumerate(values):
            self._check_type(set(value))
            value = [v.value if isinstance(v, Set) else v for v in value]
        return reduce(op, values)

    def add(self, value):
        self.update([value])

    def update(self, *values):
        self.sadd(*self._reduce(ior, values))

    def pop(self):
        return self.spop()

    def clear(self):
        self.delete()

    def remove(self, value):
        self._check_type(value)
        if self.srem(value) == 0:
            raise KeyError

    def discard(self, value):
        try:
            self.remove(value)
        except KeyError:
            pass

    def __len__(self):
        return self.scard()

    def __contains__(self, value):
        return self.sismember(value)

    def __and__(self, value):
        return self.intersection(value)

    def __iand__(self, value):
        self.intersection_update(value)
        return self

    def __rand__(self, value):
        if isinstance(value, Set):
            return value & self
        else:
            return value & self.value

    def intersection(self, *values):
        if self._all_redis(values):
            keys = [value.key for value in values]
            return self.sinter(*keys)
        else:
            return self._reduce(iand, (self.value,) + values)

    def intersection_update(self, *values):
        if self._all_redis(values):
            keys = [value.key for value in values]
            self.sinterstore(self.key, *keys)
        else:
            values = list(self._reduce(iand, values))
            self.set_intersection_update(*values)
        return self

    def __or__(self, value):
        return self.union(value)

    def __ior__(self, value):
        self.update(value)
        return self

    def __ror__(self, value):
        if isinstance(value, Set):
            return value | self
        else:
            return value | self.value

    def union(self, *values):
        if self._all_redis(values):
            keys = [value.key for value in values]
            return self.sunion(*keys)
        else:
            return self._reduce(ior, (self.value,) + values)

    def __sub__(self, value):
        return self.difference(value)

    def __isub__(self, value):
        self.difference_update(value)
        return self

    def __rsub__(self, value):
        if isinstance(value, Set):
            return value - self
        else:
            return value - self.value

    def difference(self, *values):
        if self._all_redis(values):
            keys = [value.key for value in values]
            return self.sdiff(*keys)
        else:
            return self._reduce(isub, (self.value,) + values)

    def difference_update(self, *values):
        if self._all_redis(values):
            keys = [value.key for value in values]
            self.sdiffstore(self.key, *keys)
        else:
            values = list(self._reduce(isub, values))
            self.set_difference_update(*values)
        return self

    def __xor__(self, value):
        raise NotImplemented
    def symmetric_difference(self, value):
        raise NotImplemented
    def symmetric_difference_update(self, value):
        raise NotImplemented

    def issubset(self, value):
        return self <= value

    def issuperset(self, value):
        return self >= value

    def isdisjoint(self, value):
        return not self.intersection(value)


class Dict(Iterable):

    def __init__(self, value=None, key=None):
        super(Dict, self).__init__(value, key)
        if not isinstance(value, dict):
            try:
                value = dict(value)
            except TypeError:
                value = None
        if value:
            self.update(value)

    @property
    def value(self):
        return self.hgetall()

    def update(self, value):
        self.hmset(value)

    def keys(self):
        return self.hkeys()

    def values(self):
        return self.values()

    def items(self):
        return self.value.items()

    def setdefault(self, name, value=None):
        if self.hsetnx(name, value) == 1:
            return value

    def get(self, name, default=None):
        return self.hget(name) or default

    def __getitem__(self, name):
        value = self.get(name)
        if value is None:
            raise KeyError

    def __setitem__(self, name, value):
        self.hset(name)

    def __delitem__(self, name):
        self.hdel(name)
