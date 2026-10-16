"""The registry of wrapped native package managers."""

from typing import TYPE_CHECKING, Literal

from .maven import Maven
from .npm import Npm
from .pnpm import Pnpm
from .pypi import Build, Pip, Twine, Uv


if TYPE_CHECKING:
    from .adapter import NativeAdapter


NativeTool = Literal["pip", "uv", "twine", "python", "npm", "pnpm", "mvn"]

ADAPTERS: dict[NativeTool, NativeAdapter] = {
    "pip": Pip(),
    "uv": Uv(),
    "twine": Twine(),
    "python": Build(),
    "npm": Npm(),
    "pnpm": Pnpm(),
    "mvn": Maven(),
}


def adapter_for(tool: NativeTool) -> NativeAdapter:
    return ADAPTERS[tool]
