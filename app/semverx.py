"""自带的 SemVer 解析与范围匹配引擎（npm 语义的常用子集）。

支持的范围语法：
  * 精确版本：``1.2.3``
  * 比较符：``=`` ``>`` ``>=`` ``<`` ``<=``
  * 波浪/插入：``~1.2.3`` ``^1.2.3``
  * 部分版本与 x-range：``1`` ``1.2`` ``1.x`` ``*`` ``1.2.*``
  * 连字符范围：``1.2.0 - 2.0.0``
  * 与（空格/逗号连接）、或（``||`` 连接）：``>=1.0.0 <2.0.0``

预发布（prerelease）采用 npm 的判定规则：只有当某个比较符本身带
预发布，且其 major.minor.patch 核心版本相同时，候选预发布版本才允许通过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# 版本解析
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_PARTIAL_RE = re.compile(
    r"^v?(0|[1-9]\d*|x|X|\*)(?:\.(0|[1-9]\d*|x|X|\*))?(?:\.(0|[1-9]\d*|x|X|\*))?"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_OP_RE = re.compile(r"^(>=|<=|~>|<|>|=|\^|~|v)?\s*(.*)$", re.S)


class SemverError(ValueError):
    """非法版本号或版本范围。"""


def _split_identifiers(text: str) -> List:
    """预发布标识拆成可比较的元组（数字按整数，其余按字符串）。"""
    out: List = []
    for part in text.split("."):
        if part.isdigit():
            out.append((0, int(part)))  # 数字标识优先级低于非数字
        else:
            out.append((1, part))
    return out


@dataclass(frozen=True)
class Version:
    """解析后的语义版本，build metadata 不参与比较。"""

    major: int
    minor: int
    patch: int
    prerelease: Tuple = ()
    raw: str = ""

    @classmethod
    def parse(cls, text: str) -> "Version":
        if not isinstance(text, str):
            raise SemverError(f"版本必须是字符串: {text!r}")
        m = _VERSION_RE.match(text.strip())
        if not m:
            raise SemverError(f"非法语义版本: {text!r}")
        pre = tuple(_split_identifiers(m.group(4))) if m.group(4) else ()
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre, text.strip())

    @property
    def core(self) -> Tuple[int, int, int]:
        return self.major, self.minor, self.patch

    def _key(self):
        # 有预发布 < 无预发布：用有无标记区分
        return (
            self.major,
            self.minor,
            self.patch,
            0 if self.prerelease else 1,
            self.prerelease,
        )

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __le__(self, other: "Version") -> bool:
        return self._key() <= other._key()

    def __gt__(self, other: "Version") -> bool:
        return self._key() > other._key()

    def __ge__(self, other: "Version") -> bool:
        return self._key() >= other._key()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            # prerelease 元组形如 ((0, 1), (1, 'alpha'))
            base += "-" + ".".join(str(p[1]) for p in self.prerelease)
        return base


# ---------------------------------------------------------------------------
# 范围解析：token -> (比较符, 边界版本, 核心门禁)
#
# 每个 atom 被展开为一组「界」的与关系。每个界包含：
#   op        : >、>=、<、<=、=
#   version   : 边界版本
#   gate      : None 表示无门禁；三元组表示候选必须是该核心的预发布
# ---------------------------------------------------------------------------


def _parse_partial(token: str):
    """解析可能是部分版本（1 / 1.2 / 1.x）的 token，返回 (M, m, p, pre)，通配位为 None。"""
    m = _PARTIAL_RE.match(token.strip())
    if not m:
        raise SemverError(f"非法版本范围片段: {token!r}")

    def conv(g):
        if g is None or g in ("x", "X", "*"):
            return None
        return int(g)

    major, minor, patch = conv(m.group(1)), conv(m.group(2)), conv(m.group(3))
    # 1.2.3 之后不能出现通配；1.x.3 这种“通配后又有数字”不允许
    if major is None and (minor is not None or patch is not None):
        raise SemverError(f"非法的 x-range: {token!r}")
    if minor is None and patch is not None:
        raise SemverError(f"非法的 x-range: {token!r}")
    pre = tuple(_split_identifiers(m.group(4))) if m.group(4) else ()
    # 预发布只允许出现在完整版本上
    if pre and (major is None or minor is None or patch is None):
        raise SemverError(f"预发布只能用于完整版本: {token!r}")
    return major, minor, patch, pre


def _make_version(major: int, minor: int, patch: int, pre=()) -> Version:
    return Version(major, minor, patch, tuple(pre))


def _expand_terms(op: Optional[str], token: str):
    """把单个 atom（操作符 + token）展开为界列表。返回 [(op, Version, gate_or_None), ...]。"""
    major, minor, patch, pre = _parse_partial(token)

    # 通配位置
    m_wild = major is None
    p_wild = minor is None  # 只给了 major（1 / 1.x）
    q_wild = patch is None and not m_wild and not p_wild  # 只给了 major.minor（1.2 / 1.2.x）
    full_core = not m_wild and not p_wild and not q_wild
    gate = (major, minor, patch) if full_core and pre else None

    if op in (None, "=", "v", ""):
        if m_wild:
            return []  # * 恒真
        if p_wild:
            return [(">=", _make_version(major, 0, 0), None),
                    ("<", _make_version(major + 1, 0, 0), None)]
        if q_wild:
            return [(">=", _make_version(major, minor, 0), None),
                    ("<", _make_version(major, minor + 1, 0), None)]
        return [("=", _make_version(major, minor, patch, pre), gate)]

    if op == ">=":
        if m_wild:
            return []
        if p_wild:
            return [(">=", _make_version(major, 0, 0), None)]
        if q_wild:
            return [(">=", _make_version(major, minor, 0), None)]
        return [(">=", _make_version(major, minor, patch, pre), gate)]

    if op == "<=":
        if m_wild:
            return []
        if p_wild:
            # <=1 → 整个 major 段
            return [("<", _make_version(major + 1, 0, 0), None)]
        if q_wild:
            # <=1.2 → 整个 minor 段
            return [("<", _make_version(major, minor + 1, 0), None)]
        return [("<=", _make_version(major, minor, patch, pre), gate)]

    if op == ">":
        if m_wild:
            return [("<", _make_version(0, 0, 0), None)]  # >* 不可满足
        if p_wild:
            return [(">=", _make_version(major + 1, 0, 0), None)]
        if q_wild:
            return [(">=", _make_version(major, minor + 1, 0), None)]
        return [(">", _make_version(major, minor, patch, pre), gate)]

    if op == "<":
        if m_wild:
            return [("<", _make_version(0, 0, 0), None)]  # <* 不可满足
        if p_wild:
            return [("<", _make_version(major, 0, 0), None)]
        if q_wild:
            return [("<", _make_version(major, minor, 0), None)]
        return [("<", _make_version(major, minor, patch, pre), gate)]

    if op == "~":
        if m_wild:
            return []
        if p_wild:
            # ~1 := >=1.0.0 <2.0.0
            return [(">=", _make_version(major, 0, 0), None),
                    ("<", _make_version(major + 1, 0, 0), None)]
        if q_wild:
            # ~1.2 := >=1.2.0 <1.3.0
            return [(">=", _make_version(major, minor, 0), None),
                    ("<", _make_version(major, minor + 1, 0), None)]
        return [(">=", _make_version(major, minor, patch, pre), gate),
                ("<", _make_version(major, minor + 1, 0), None)]

    if op == "^":
        if m_wild:
            return []
        if p_wild:
            # ^1 / ^0 行为与 ~1 / ~0 一致
            return [(">=", _make_version(major, 0, 0), None),
                    ("<", _make_version(major + 1, 0, 0), None)]
        if q_wild:
            # ^1.2 := >=1.2.0 <2.0.0；^0.x 段允许到 1.0.0
            if major > 0:
                upper = _make_version(major + 1, 0, 0)
            else:
                upper = _make_version(0, minor + 1, 0)
            return [(">=", _make_version(major, minor, 0), None),
                    ("<", upper, None)]
        lower = ("^>=", _make_version(major, minor, patch, pre), gate)
        if major > 0:
            upper = _make_version(major + 1, 0, 0)
        elif minor > 0:
            upper = _make_version(0, minor + 1, 0)
        else:
            upper = _make_version(0, 0, patch + 1)
        return [lower, ("<", upper, None)]

    raise SemverError(f"不支持的操作符: {op!r}")


# ---------------------------------------------------------------------------
# RangeSpec
# ---------------------------------------------------------------------------


def _expand_hyphen(token: str):
    """展开 ``a - b`` 连字符范围，返回左右两个 atom 字符串。"""
    left, right = token.split(" - ", 1)
    left, right = left.strip(), right.strip()
    lm, ln, lp, lpre = _parse_partial(left)
    rm, rn, rp, rpre = _parse_partial(right)
    if lpre or rpre:
        raise SemverError(f"连字符范围不支持预发布: {token!r}")

    # 左边界：部分版本取该段下界
    if lm is None:
        lo = (">=", _make_version(0, 0, 0), None)
    elif ln is None:
        lo = (">=", _make_version(lm, 0, 0), None)
    elif lp is None:
        lo = (">=", _make_version(lm, ln, 0), None)
    else:
        lo = (">=", _make_version(lm, ln, lp), (lm, ln, lp))

    # 右边界：部分版本取该段上界（不含），完整版本精确含
    if rm is None:
        terms = [lo]
    elif rn is None:
        terms = [lo, ("<", _make_version(rm + 1, 0, 0), None)]
    elif rp is None:
        terms = [lo, ("<", _make_version(rm, rn + 1, 0), None)]
    else:
        terms = [lo, ("<=", _make_version(rm, rn, rp), (rm, rn, rp))]
    return terms


def _split_atoms(text: str) -> List[str]:
    """把一个 AND 组拆成 atom 列表，兼容空格与逗号。

    连字符范围（``a - b``，连字符两侧有空格）作为一个整体保留；
    普通空格与逗号是 AND 连接符。
    """
    text = text.replace(",", " ")
    out: List[str] = []
    for raw in text.split():
        if raw == "-" and out:
            # 与前后 token 合并为 "a - b"
            out[-1] = out[-1] + " - "
        elif out and out[-1].endswith(" - "):
            out[-1] = out[-1] + raw
        else:
            out.append(raw)
    return out


@dataclass(frozen=True)
class RangeSpec:
    """一个完整的版本范围（OR-of-AND 比较符）。"""

    # 每个 AND 组：[(op, version, gate), ...]
    groups: Tuple[Tuple, ...]
    raw: str

    @classmethod
    def parse(cls, spec: str) -> "RangeSpec":
        if spec is None:
            spec = "*"
        if not isinstance(spec, str):
            raise SemverError(f"范围必须是字符串: {spec!r}")
        raw = spec.strip()
        if raw == "":
            raw = "*"

        groups = []
        for or_part in re.split(r"\|\|", raw):
            or_part = or_part.strip()
            if or_part == "":
                raise SemverError(f"非法的 OR 范围: {spec!r}")
            terms: List = []
            for atom in _split_atoms(or_part):
                if " - " in atom:
                    terms.extend(_expand_hyphen(atom))
                    continue
                m = _OP_RE.match(atom)
                op, token = m.group(1), m.group(2).strip()
                if token == "":
                    raise SemverError(f"范围片段缺少版本号: {atom!r}")
                expanded = _expand_terms(op, token)
                # ^ 的下界用 >= 语义
                for eop, ever, egate in expanded:
                    terms.append((">=" if eop == "^>=" else eop, ever, egate))
            groups.append(tuple(terms))
        return cls(tuple(groups), raw)

    def satisfies(self, ver: Version) -> bool:
        return any(_group_satisfies(group, ver) for group in self.groups)

    def __str__(self) -> str:
        return self.raw


_OPS = {
    ">": lambda v, b: v > b,
    ">=": lambda v, b: v >= b,
    "<": lambda v, b: v < b,
    "<=": lambda v, b: v <= b,
    "=": lambda v, b: v == b,
}


def _group_satisfies(group, ver: Version) -> bool:
    gates = []
    for op, bound, gate in group:
        if gate is not None and ver.prerelease and ver.core != gate:
            return False
        if not _OPS[op](ver, bound):
            return False
        if gate is not None:
            gates.append(gate)
    # 预发布候选：至少有一个比较符的核心门禁与候选核心相同
    if ver.prerelease and not gates:
        return False
    return True
