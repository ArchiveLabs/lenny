
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Literal
from datetime import datetime

OPDS_REL_ACQUISITION = "http://opds-spec.org/acquisition/open-access"

@dataclass
class Author:
    name: str
    
    def to_dict(self):
        return asdict(self)

@dataclass
class Link:
    href: str
    type: str
    rel: Optional[str] = None

@dataclass
class Publication:
    metadata: dict
    links: List[Link]

@dataclass
class OPDSFeed:
    metadata: dict
    publications: List[Publication]

    def to_dict(self):
        return asdict(self)
