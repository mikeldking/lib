from collections import deque
from copy import copy
from functools import partial
from typing import Awaitable, Callable, Generic, TypeVar, Union, get_origin
import dataclasses
import inspect
import json
import yaml


from zenbase.utils import asyncify, ksuid_generator, syncify


class Dataclass:
    """
    Modified from Braintrust's SerializableDataClass
    """

    def copy(self, **changes):
        return dataclasses.replace(self, **changes)

    def as_dict(self):
        """Serialize the object to a dictionary."""
        return dataclasses.asdict(self)

    def as_json(self, **kwargs):
        """Serialize the object to JSON."""
        return json.dumps(self.as_dict(), **kwargs)

    def as_yaml(self):
        """Serialize the object to YAML."""
        return yaml.dumps(self.as_dict())

    @classmethod
    def from_dict(cls, d: dict):
        """Deserialize the object from a dictionary. This method
        is shallow and will not call from_dict() on nested objects."""
        fields = set(f.name for f in dataclasses.fields(cls))
        filtered = {k: v for k, v in d.items() if k in fields}
        return cls(**filtered)

    @classmethod
    def from_dict_deep(cls, d: dict):
        """Deserialize the object from a dictionary. This method
        is deep and will call from_dict_deep() on nested objects."""
        fields = {f.name: f for f in dataclasses.fields(cls)}
        filtered = {}
        for k, v in d.items():
            if k not in fields:
                continue

            if (
                isinstance(v, dict)
                and isinstance(fields[k].type, type)
                and issubclass(fields[k].type, Dataclass)
            ):
                filtered[k] = fields[k].type.from_dict_deep(v)
            elif get_origin(fields[k].type) == Union:
                for t in fields[k].type.__args__:
                    if isinstance(t, type) and issubclass(t, Dataclass):
                        try:
                            filtered[k] = t.from_dict_deep(v)
                            break
                        except TypeError:
                            pass
                else:
                    filtered[k] = v
            elif (
                isinstance(v, list)
                and get_origin(fields[k].type) == list
                and len(fields[k].type.__args__) == 1
                and isinstance(fields[k].type.__args__[0], type)
                and issubclass(fields[k].type.__args__[0], Dataclass)
            ):
                filtered[k] = [fields[k].type.__args__[0].from_dict_deep(i) for i in v]
            else:
                filtered[k] = v
        return cls(**filtered)


Inputs = TypeVar("Inputs", covariant=True, bound=dict)
Outputs = TypeVar("Outputs", covariant=True, bound=dict)


@dataclasses.dataclass(frozen=True)
class LMDemo(Dataclass, Generic[Inputs, Outputs]):
    inputs: Inputs
    outputs: Outputs

    def __hash__(self):
        return hash((frozenset(self.inputs.items()), frozenset(self.outputs.items())))


@dataclasses.dataclass(frozen=True)
class LMZenbase(Dataclass, Generic[Inputs, Outputs]):
    task_demos: list[LMDemo[Inputs, Outputs]] = dataclasses.field(default_factory=list)
    model_params: dict = dataclasses.field(
        default_factory=dict
    )  # OpenAI-compatible model params


@dataclasses.dataclass(frozen=True)
class LMRequest(Dataclass, Generic[Inputs, Outputs]):
    zenbase: LMZenbase[Inputs, Outputs]
    inputs: Inputs = dataclasses.field(default_factory=dict)
    id: str = dataclasses.field(default_factory=ksuid_generator("request"))


@dataclasses.dataclass(frozen=True)
class LMResponse(Dataclass, Generic[Outputs]):
    outputs: Outputs
    attributes: dict = dataclasses.field(
        default_factory=dict
    )  # token_count, cost, inference_time, etc.
    id: str = dataclasses.field(default_factory=ksuid_generator("response"))


@dataclasses.dataclass(frozen=True)
class LMCall(Dataclass, Generic[Inputs, Outputs]):
    function: "LMFunction[Inputs, Outputs]"
    request: LMRequest[Inputs, Outputs]
    response: LMResponse[Outputs]
    id: str = dataclasses.field(default_factory=ksuid_generator("call"))


SyncDef = Callable[
    [LMRequest[Inputs, Outputs]],
    Outputs,
]

AsyncDef = Callable[
    [LMRequest[Inputs, Outputs]],
    Awaitable[Outputs],
]


class LMFunction(Generic[Inputs, Outputs]):
    gen_id = staticmethod(ksuid_generator("fn"))

    id: str
    fn: SyncDef[Inputs, Outputs] | AsyncDef[Inputs, Outputs]
    __name__: str
    __qualname__: str
    __doc__: str
    __signature__: inspect.Signature
    zenbase: LMZenbase[Inputs, Outputs]
    history: deque[LMCall[Inputs, Outputs]]

    def __init__(
        self,
        fn: SyncDef[Inputs, Outputs] | AsyncDef[Inputs, Outputs],
        zenbase: LMZenbase | None = None,
        maxhistory: int = 1,
    ):
        self.fn = fn

        if qualname := getattr(fn, "__qualname__", None):
            self.id = qualname
            self.__qualname__ = qualname
        else:
            self.id = self.gen_id()
            self.__qualname__ = f"zenbase_{self.id}"

        self.__name__ = getattr(fn, "__name__", f"zenbase_{self.id}")

        self.__doc__ = getattr(fn, "__doc__", "")
        self.__signature__ = inspect.signature(fn)

        self.zenbase = zenbase or LMZenbase()
        self.history = deque([], maxlen=maxhistory)

    def refine(self, zenbase: LMZenbase | None = None) -> "LMFunction[Inputs, Outputs]":
        dup = copy(self)
        dup.id = self.gen_id()
        dup.zenbase = zenbase or self.zenbase.copy()
        dup.history = deque([], maxlen=self.history.maxlen)
        return dup

    def prepare_request(self, inputs: Inputs) -> LMRequest[Inputs, Outputs]:
        return LMRequest(zenbase=self.zenbase, inputs=inputs)

    def process_response(
        self,
        request: LMRequest[Inputs, Outputs],
        outputs: Outputs,
    ) -> Outputs:
        self.history.append(LMCall(self, request, LMResponse(outputs)))
        return outputs

    def __call__(self, inputs: Inputs, *args, **kwargs) -> Outputs:
        request = self.prepare_request(inputs)
        response = syncify(self.fn)(request, *args, **kwargs)
        return self.process_response(request, response)

    async def coroutine(
        self,
        inputs: Inputs,
        *args,
        **kwargs,
    ) -> Outputs:
        request = self.prepare_request(inputs)
        response = await asyncify(self.fn)(request, *args, **kwargs)
        return self.process_response(request, response)


def deflm(
    function: SyncDef[Inputs, Outputs] | AsyncDef[Inputs, Outputs] | None = None,
    zenbase: LMZenbase[Inputs, Outputs] | None = None,
) -> LMFunction[Inputs, Outputs]:
    if function is None:
        return partial(deflm, zenbase=zenbase)

    if isinstance(function, LMFunction):
        return function.refine(zenbase)

    return LMFunction(function, zenbase)
