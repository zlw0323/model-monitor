"""Parse pasted tables / CSV files of models for bulk import."""

import csv
import io
import math
from dataclasses import asdict, dataclass

MAX_ROWS = 1000

# Accepted header names (lower-cased, spaces removed) for each field.
HEADERS = {
    "resource": ("资源", "资源名称", "azure资源", "resource", "resourcename"),
    "name": ("名称", "显示名称", "模型名称", "name", "displayname"),
    "kind": ("类型", "模型类型", "kind", "type"),
    "deployment": ("部署", "部署名称", "部署名", "deployment", "deploymentname"),
    "interval": ("间隔", "检测间隔", "检测间隔(分钟)", "检测间隔（分钟）", "interval", "intervalminutes", "interval_minutes"),
    "threshold": ("阈值", "预警阈值", "ttft预警阈值", "预警阈值%", "threshold", "ttft_threshold_pct"),
    "baseline": ("手动基线", "手动基线ms", "baseline", "baseline_manual_ms"),
    "enabled": ("启用", "是否启用", "enabled"),
}
KIND_VALUES = {
    "text": ("text", "文本", "推理", "文本模型", "推理模型", "文本/推理", "chat", "llm"),
    "image": ("image", "图像", "图片", "图像模型", "图片模型", "img"),
}
TRUE_VALUES = ("1", "true", "yes", "y", "是", "启用", "开")
FALSE_VALUES = ("0", "false", "no", "n", "否", "停用", "关")
TEMPLATE = "资源,名称,类型,部署名称,检测间隔,预警阈值,手动基线\nmyres,GPT-5 美东2,文本,gpt-5,30,,\nmyres,gpt-image-1 美东2,图像,gpt-image-1,30,100,\n"


@dataclass
class ImportRow:
    line: int
    resource: str
    name: str
    kind: str
    deployment: str
    interval: int | None
    threshold: float | None
    baseline: float | None
    enabled: bool
    resource_id: int | None = None
    status: str = "ok"  # ok | duplicate | error
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(s: str) -> str:
    return "".join((s or "").split()).lower().lstrip("\ufeff")


def _header_map(row: list[str]) -> dict[str, int] | None:
    lookup = {alias: field for field, aliases in HEADERS.items() for alias in aliases}
    found = {}
    for i, cell in enumerate(row):
        field = lookup.get(_norm(cell))
        if field and field not in found:
            found[field] = i
    return found if len(found) >= 2 else None


def _number(raw: str, kind: type, lo: float, hi: float, label: str):
    raw = raw.strip().rstrip("%").replace("，", "").replace(",", "")
    if not raw:
        return None
    try:
        v = kind(float(raw)) if kind is int else float(raw)
    except (ValueError, OverflowError):
        raise ValueError(f"{label}「{raw}」不是有效数字")
    if not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{label}必须在 {lo:g}~{hi:g} 之间")
    return v


def parse(text: str, resources: dict[str, int], existing: set[tuple[int, str, str]], default_interval: int) -> list[ImportRow]:
    """`resources` maps lower-cased resource name -> id; `existing` holds (resource_id, deployment.lower(), kind)."""
    text = (text or "").lstrip("\ufeff").strip()
    if not text:
        raise ValueError("内容为空")
    first = text.splitlines()[0]
    delimiter = "\t" if "\t" in first else ("；" if "；" in first else (";" if ";" in first and "," not in first else ","))
    table = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    header = _header_map(table[0])
    if not header:
        raise ValueError("第一行必须是表头，至少包含“资源”“类型”“部署名称”三列（可下载模板参考）")
    missing = [label for field, label in (("resource", "资源"), ("kind", "类型"), ("deployment", "部署名称")) if field not in header]
    if missing:
        raise ValueError(f"表头缺少列：{'、'.join(missing)}")
    body = table[1:]
    if len(body) > MAX_ROWS:
        raise ValueError(f"一次最多导入 {MAX_ROWS} 行")

    def cell(row: list[str], field: str) -> str:
        i = header.get(field)
        return row[i].strip() if i is not None and i < len(row) else ""

    seen: set[tuple[int, str, str]] = set()
    out: list[ImportRow] = []
    for n, row in enumerate(body, start=2):
        r = ImportRow(line=n, resource=cell(row, "resource"), name=cell(row, "name"), kind=cell(row, "kind"),
                      deployment=cell(row, "deployment"), interval=None, threshold=None, baseline=None, enabled=True)
        out.append(r)
        try:
            if not r.resource:
                raise ValueError("缺少资源")
            r.resource_id = resources.get(r.resource.lower())
            if r.resource_id is None:
                raise ValueError(f"资源「{r.resource}」不存在，请先在“资源”页创建")
            kind = _norm(r.kind)
            r.kind = next((k for k, vals in KIND_VALUES.items() if kind in vals), "")
            if not r.kind:
                raise ValueError(f"类型「{cell(row, 'kind')}」无法识别，请填写 文本 或 图像")
            if not r.deployment:
                raise ValueError("缺少部署名称")
            if len(r.deployment) > 200 or len(r.name) > 100:
                raise ValueError("部署名称或名称过长")
            r.name = r.name or r.deployment
            r.interval = _number(cell(row, "interval"), int, 1, 1440, "检测间隔") or default_interval
            r.threshold = _number(cell(row, "threshold"), float, 1, 10000, "预警阈值")
            r.baseline = _number(cell(row, "baseline"), float, 1, 3_600_000, "手动基线")
            en = _norm(cell(row, "enabled"))
            if en:
                if en not in TRUE_VALUES + FALSE_VALUES:
                    raise ValueError(f"启用列「{cell(row, 'enabled')}」请填写 是 或 否")
                r.enabled = en in TRUE_VALUES
            key = (r.resource_id, r.deployment.lower(), r.kind)
            if key in existing:
                r.status, r.message = "duplicate", "已存在（同一资源、部署名称和类型），跳过"
            elif key in seen:
                r.status, r.message = "duplicate", "与前面的行重复，跳过"
            seen.add(key)
        except ValueError as e:
            r.status, r.message = "error", str(e)
    return out
