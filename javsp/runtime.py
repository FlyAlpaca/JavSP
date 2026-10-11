"""单批次运行资源，与冻结的用户配置分开持有。"""

from dataclasses import dataclass
from pathlib import Path

from javsp.config import Cfg
from javsp.metadata.actors import ActorMapping


@dataclass
class RuntimeContext:
    cfg: Cfg
    base_dir: Path
    root: Path | None = None
    database_path: Path | None = None
    actor_mapping: ActorMapping | None = None
    translator: object | None = None

    @classmethod
    def create(cls, cfg, base_dir=None):
        return cls(cfg, Path(base_dir or Path.cwd()).resolve())

    @property
    def actor_mapping_path(self):
        return self.base_dir / 'config/actor-mapping.xml'

    @property
    def database_cache_path(self):
        return self.base_dir / 'config/r18dev.sqlite'
