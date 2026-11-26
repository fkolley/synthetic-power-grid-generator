"""
Format-neutral CIM object records and their two serializations:

* CIM17 (IEC 61970-301 CIM17, namespace CIM100#) through cim-graph, one RDF/XML file
  with typed SI units.
* CGMES 2.4.15 (ENTSO-E, namespace CIM16#) written directly: one file per profile
  (EQ, TP, SSH, SV, GL, DL) in a zip, values in CGMES units (kV, MW, MVAr, km ...).

Records hold values in CGMES units. Attribute names are the short CIM names
(e.g. "length"); the CGMES writer qualifies them with the declaring class from the
CIM16 schema (e.g. "Conductor.length"), the CIM17 writer converts values to typed
cim-graph units.
"""

import datetime
import os
import re
import uuid
import zipfile
from dataclasses import dataclass, field, fields
from typing import Dict, List, Optional
from xml.sax.saxutils import escape

# The profile must be selected before cim-graph is imported.
os.environ["CIMG_CIM_PROFILE"] = "cimprofiles.cim17.cim17v40"
os.environ["CIMG_USE_UNITS"] = "true"

import cimprofiles.cim17.cim17v40 as cim
from cimprofiles.cim16.cim16v33 import cim16v33 as cim16
from cimgraph import utils
from cimgraph.core.attribute_utils import get_attr_declaring_class, get_attr_inverse, get_attr_serialize
from cimgraph.models import GraphModel

# Profile tags
EQ, TP, SSH, SV, GL, DL = "EQ", "TP", "SSH", "SV", "GL", "DL"
CIM17_ONLY = "CIM17"           # attribute written to the CIM17 file only
CGMES_PROFILES = (EQ, TP, SSH, SV, GL, DL)


@dataclass(frozen=True)
class Ref:
    """Reference to another record."""
    id: str


@dataclass(frozen=True)
class Enum:
    kind: str
    value: str
    ns: str = "cim"            # "cim" or "entsoe" (CGMES extension, skipped in CIM17)


@dataclass
class Record:
    cls: str
    id: str
    profile: str               # profile that defines the object (rdf:ID)
    attrs: List[tuple] = field(default_factory=list)   # (profile, name, value)

    def set(self, name: str, value, profile: Optional[str] = None) -> "Record":
        if value is not None:
            self.attrs.append((profile or self.profile, name, value))
        return self

    def get(self, name: str):
        return next((v for _, n, v in self.attrs if n == name), None)


class IdFactory:
    """Deterministic UUIDv4 mRIDs from a seed."""

    def __init__(self, seed):
        import random
        self._rng = random.Random(f"mrid-{seed}")

    def __call__(self) -> str:
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))


class RecordSet:
    def __init__(self, seed):
        self.seed = seed
        self._new_id = IdFactory(seed)
        self.records: List[Record] = []

    def new(self, cls: str, profile: str, name: Optional[str] = None) -> Record:
        rec = Record(cls, self._new_id(), profile)
        self.records.append(rec)
        if name is not None:
            rec.set("name", name)
        return rec

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for r in self.records:
            out[r.cls] = out.get(r.cls, 0) + 1
        return out


# ---------------------------------------------------------------------------
# CIM17 via cim-graph
# ---------------------------------------------------------------------------

# CIM17 unit class -> unit of the record value (CGMES units)
_UNIT_OF = {
    "Voltage": "kV", "ActivePower": "MW", "ReactivePower": "MVAr", "ApparentPower": "MVA",
    "Resistance": "ohm", "Reactance": "ohm", "Susceptance": "S", "Conductance": "S",
    "Length": "km", "CurrentFlow": "A", "AngleDegrees": "deg", "Seconds": "s",
}
# Attributes of the CGMES 2.4.15 profiles that CIM17 dropped
_CGMES_ONLY = {("RegulatingControl", "discrete")}
_TYPE_UNIT = re.compile(r"\|\s*(\w+)\]")
_field_cache: Dict[type, Dict[str, str]] = {}


def _cim17_fields(cls) -> Dict[str, str]:
    if cls not in _field_cache:
        _field_cache[cls] = {f.name: f.type for f in fields(cls)}
    return _field_cache[cls]


def _cim17_value(value, ftype: str, objs: Dict):
    if isinstance(value, list):
        return [_cim17_value(v, ftype, objs) for v in value]
    if isinstance(value, Ref):
        return objs[value.id]
    if isinstance(value, Enum):
        return getattr(getattr(cim, value.kind), value.value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        m = _TYPE_UNIT.search(ftype)
        if m and m.group(1) != "float" and hasattr(cim, m.group(1)):
            unit_cls = m.group(1)
            if unit_cls not in _UNIT_OF:
                raise KeyError(f"no unit mapping for CIM17 type {unit_cls}")
            return getattr(cim, unit_cls)(float(value), _UNIT_OF[unit_cls])
    return value


def to_cimgraph(records: List[Record]) -> GraphModel:
    """Build an in-memory cim-graph model (CIM17) from records."""
    graph: Dict = {}
    objs = {r.id: utils.create_object(getattr(cim, r.cls), r.id, graph) for r in records}
    for r in records:
        obj = objs[r.id]
        flds = _cim17_fields(type(obj))
        for _profile, name, value in r.attrs:
            if ":" in name or (isinstance(value, Enum) and value.ns != "cim"):
                continue                                  # CGMES extension namespace
            if name not in flds:
                if (r.cls, name) in _CGMES_ONLY:
                    continue
                raise KeyError(f"CIM17 class {r.cls} has no attribute {name}")
            v = _cim17_value(value, flds[name], objs)
            if flds[name].startswith("list") and not isinstance(v, list):
                v = [v]
            setattr(obj, name, v)
            # cim-graph writes each association from one end only; if this end is not
            # serialized, also set the inverse on the referenced object(s).
            inverse = get_attr_inverse(type(obj), name)
            if inverse and not get_attr_serialize(type(obj), name):
                inv_name = inverse.split(".")[-1]
                for target in (v if isinstance(v, list) else [v]):
                    if _cim17_fields(type(target))[inv_name].startswith("list"):
                        getattr(target, inv_name).append(obj)
                    else:
                        setattr(target, inv_name, obj)
    return GraphModel(container=None, connection=None, graph=graph)


def write_cim17(records: List[Record], filename: str) -> GraphModel:
    model = to_cimgraph(records)
    utils.write_xml(model, filename)
    return model


# ---------------------------------------------------------------------------
# CGMES 2.4.15
# ---------------------------------------------------------------------------

CGMES_NS = {
    "cim": "http://iec.ch/TC57/2013/CIM-schema-cim16#",
    "entsoe": "http://entsoe.eu/CIM/SchemaExtension/3/1#",
    "md": "http://iec.ch/TC57/61970-552/ModelDescription/1#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}
PROFILE_URIS = {
    EQ: ["http://entsoe.eu/CIM/EquipmentCore/3/1", "http://entsoe.eu/CIM/EquipmentOperation/3/1"],
    TP: ["http://entsoe.eu/CIM/Topology/4/1"],
    SSH: ["http://entsoe.eu/CIM/SteadyStateHypothesis/1/1"],
    SV: ["http://entsoe.eu/CIM/StateVariables/4/1"],
    GL: ["http://entsoe.eu/CIM/GeographicalLocation/2/1"],
    DL: ["http://entsoe.eu/CIM/DiagramLayout/3/1"],
}
DEPENDS_ON = {TP: [EQ], SSH: [EQ], SV: [TP, SSH], GL: [EQ], DL: [EQ]}
MAX_NAME_LEN = 32              # CGMES 2.4.15 rule for IdentifiedObject.name
MODELING_AUTHORITY = "http://synthetic-grid.local/"


def _qname(cls: str, name: str) -> str:
    if ":" in name:
        return name                                       # already qualified (entsoe:...)
    declaring = get_attr_declaring_class(getattr(cim16, cls), name)
    return f"cim:{declaring.__name__}.{name}"


def _text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.10g}"
    return escape(str(value))


def _property_xml(tag: str, value) -> str:
    if isinstance(value, list):
        return "".join(_property_xml(tag, v) for v in value)
    if isinstance(value, Ref):
        return f'    <{tag} rdf:resource="#_{value.id}"/>\n'
    if isinstance(value, Enum):
        return f'    <{tag} rdf:resource="{CGMES_NS[value.ns]}{value.kind}.{value.value}"/>\n'
    return f"    <{tag}>{_text(value)}</{tag}>\n"


def _profile_xml(records: List[Record], profile: str, header: Dict) -> str:
    ns = " ".join(f'xmlns:{p}="{u}"' for p, u in CGMES_NS.items())
    out = [f'<?xml version="1.0" encoding="utf-8"?>\n<rdf:RDF {ns}>\n']
    out.append(f'  <md:FullModel rdf:about="urn:uuid:{header["ids"][profile]}">\n')
    out.append(f'    <md:Model.scenarioTime>{header["scenario_time"]}</md:Model.scenarioTime>\n')
    out.append(f'    <md:Model.created>{header["created"]}</md:Model.created>\n')
    out.append(f'    <md:Model.description>{escape(header["description"])}</md:Model.description>\n')
    out.append("    <md:Model.version>1</md:Model.version>\n")
    for dep in DEPENDS_ON.get(profile, []):
        out.append(f'    <md:Model.DependentOn rdf:resource="urn:uuid:{header["ids"][dep]}"/>\n')
    for uri in PROFILE_URIS[profile]:
        out.append(f"    <md:Model.profile>{uri}</md:Model.profile>\n")
    out.append(f"    <md:Model.modelingAuthoritySet>{MODELING_AUTHORITY}</md:Model.modelingAuthoritySet>\n")
    out.append("  </md:FullModel>\n")
    for r in sorted(records, key=lambda r: (r.cls, r.id)):
        props = [(n, v) for p, n, v in r.attrs if p == profile]
        if r.profile == profile:
            out.append(f'  <cim:{r.cls} rdf:ID="_{r.id}">\n')
        elif props:
            out.append(f'  <cim:{r.cls} rdf:about="#_{r.id}">\n')
        else:
            continue
        for name, value in props:
            if name == "name":
                value = str(value)[:MAX_NAME_LEN]
            out.append(_property_xml(_qname(r.cls, name), value))
        out.append(f"  </cim:{r.cls}>\n")
    out.append("</rdf:RDF>\n")
    return "".join(out)


def write_cgmes(records: List[Record], filename: str, seed, description: str,
                scenario_time: datetime.datetime) -> List[str]:
    """Write one RDF/XML file per CGMES profile into a zip. Returns the file names."""
    profiles = [p for p in CGMES_PROFILES
                if any(r.profile == p or any(ap == p for ap, _, _ in r.attrs) for r in records)]
    stamp = scenario_time.strftime("%Y%m%dT%H%MZ")
    header = {
        "ids": {p: str(uuid.uuid5(uuid.NAMESPACE_URL, f"{MODELING_AUTHORITY}{seed}/{description}/{p}"))
                for p in CGMES_PROFILES},
        "scenario_time": scenario_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": description,
    }
    names = []
    with zipfile.ZipFile(filename, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in profiles:
            name = f"{stamp}_1D_SYNTH_{p}_001.xml"
            info = zipfile.ZipInfo(name, date_time=scenario_time.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16        # rw-r--r--
            zf.writestr(info, _profile_xml(records, p, header))
            names.append(name)
    return names
