"""arXiv 分类树词表（M1 画像内核 · 纯数据，零 IO 零依赖）。

规则：
- 父组由前缀推导：`cs.CL→cs`、`physics.optics→physics`、`quant-ph→quant-ph`（无点即自身成组）；
- 兄弟 = 同组已知分类；跨组桥用显式小表（不瞎猜）；
- 未知分类一律安全回退（group=自身、siblings=[]、is_known=False），调用方永不会 KeyError。
"""

from __future__ import annotations

#: 已知 arXiv 分类（扁平列表；组=前缀）。收录常规全集，漏收不致命、只降权不报错。
_CS = ("cs.AI cs.ARO cs.AT cs.CC cs.CF cs.CG cs.CI cs.CR cs.CV cs.CY cs.DB cs.DC "
       "cs.DL cs.DM cs.DS cs.ET cs.FL cs.GL cs.GN cs.GR cs.HC cs.IR cs.IT cs.LG cs.LO "
       "cs.MA cs.MM cs.MS cs.NA cs.NE cs.NI cs.OH cs.OS cs.PF cs.PL cs.RO cs.SC cs.SD "
       "cs.SI cs.SY cs.SE cs.CL cs.GT cs.HP cs.SS")
_PHYS = ("physics.acc-ph physics.ao-ph physics.app-ph physics.atm-clus physics.atom-ph "
         "physics.bio-ph physics.chem-ph physics.class-ph physics.comp-ph physics.data-an "
         "physics.ed-ph physics.flu-dyn physics.gen-ph physics.geo-ph physics.hist-ph "
         "physics.ins-det physics.med-ph physics.optics physics.plasm-ph physics.pop-ph "
         "physics.soc-ph physics.space-ph")
_MATH = ("math.AG math.AT math.CA math.CO math.CT math.DG math.DS math.FA math.GM math.GN "
         "math.GT math.HO math.IT math.KT math.LO math.MG math.MP math.NA math.NT math.OC "
         "math.PR math.QA math.RA math.RT math.SG math.SP math.ST math.OA "
         "stat.AP stat.CO stat.ME stat.ML stat.OT stat.TH "
         "nlin.AO nlin.CD nlin.CG nlin.PS nlin.SI")
_OTHER = ("quant-ph q-bio.BM q-bio.CB q-bio.GN q-bio.MN q-bio.NC q-bio.OT q-bio.PE "
          "q-bio.QM q-bio.SC q-bio.TO q-fin.CP q-fin.EC q-fin.GN q-fin.MF q-fin.PM q-fin.PR "
          "q-fin.RM q-fin.ST astro-ph.CO astro-ph.EP astro-ph.GA astro-ph.HE astro-ph.IM "
          "astro-ph.SR cond-mat.dis-nn cond-mat.mes-hall cond-mat.mtrl-sci cond-mat.other "
          "cond-mat.quant-gas cond-mat.soft cond-mat.stat-mech cond-mat.str-el "
          "gr-qc hep-ex hep-lat hep-ph hep-th eess.AS eess.IV eess.SP eess.SY")

KNOWN: tuple[str, ...] = tuple(sorted({
    *(_CS.split()), *(_PHYS.split()), *(_MATH.split()), *(_OTHER.split()),
}))

#: 跨组桥（组→邻近组）：邻接扩展道用（"技术×物理"这类桥）。显式声明，宁窄勿滥。
GROUP_BRIDGES: dict[str, tuple[str, ...]] = {
    "cs": ("stat", "eess", "math", "quant-ph"),
    "stat": ("cs", "math"),
    "math": ("stat", "cs", "nlin", "physics"),
    "nlin": ("math", "physics"),
    "quant-ph": ("physics", "cond-mat", "cs"),
    "physics": ("quant-ph", "cond-mat", "math", "eess"),
    "cond-mat": ("physics", "quant-ph"),
    "hep-th": ("gr-qc", "quant-ph", "math"),
    "hep-ph": ("hep-th", "astro-ph"),
    "gr-qc": ("hep-th", "astro-ph"),
    "astro-ph": ("gr-qc", "hep-ph"),
    "q-bio": ("physics", "stat", "cs"),
    "q-fin": ("stat", "physics", "cs"),
    "eess": ("cs", "physics"),
}

_KNOWN = frozenset(KNOWN)


def group_of(cat: str) -> str:
    """`cs.CL→cs`；`quant-ph→quant-ph`（无点者自身即组）。"""
    return (cat.split(".", 1)[0] if "." in cat else cat).strip()


def is_known(cat: str) -> bool:
    return cat in _KNOWN


def siblings_of(cat: str) -> list[str]:
    """同组已知兄弟（不含自身）；未知分类回 []（安全）。"""
    g = group_of(cat)
    return [c for c in KNOWN if c != cat and group_of(c) == g]


def bridge_groups(cat: str) -> tuple[str, ...]:
    """跨组桥的邻近组名（供邻接道扩召回）。未知组回空。"""
    return GROUP_BRIDGES.get(group_of(cat), ())
