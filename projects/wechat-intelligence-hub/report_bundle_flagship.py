#!/usr/bin/env python3
"""Sectioned Markdown site and complete flagship HTML for WeChat reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit

from report_security import protect_document, require_sanitizer, sanitize_fragment


GENERATED_NAMES = {"wechat_daily_full.md", "wechat_daily_report.md"}
MARKDOWN_SITE_DIR = "wechat-report"
GROUP_VALUE_DIMENSIONS = (
    "AI", "赚钱", "培训", "商单", "出海", "产品", "Web3",
    "自媒体运营与增长", "合作", "B端AI赋能",
)


@dataclass(frozen=True)
class ReportSource:
    path: Path
    relative_path: str
    source_id: str
    title: str
    kind: str
    priority: int
    collapsed: bool = False


@dataclass(frozen=True)
class ReportPage:
    route: str
    filename: str
    title: str
    nav_label: str
    description: str
    glyph: str
    priority: int
    sources: tuple[ReportSource, ...]


SOURCE_RULES: tuple[tuple[str, str, str, int, bool], ...] = (
    ("final_report.md", "执行结论", "overview", 10, False),
    ("action_overview.md", "行动与商机机器总览", "internal", 79, True),
    ("group-daily/group_daily_topics.md", "话题日报", "groups", 20, False),
    ("group-daily/group_daily_groups.md", "重点群聊", "groups", 21, False),
    ("group-daily/group_daily_brief.md", "群聊日报", "groups", 22, False),
    ("contact-daily/contact_daily_brief.md", "重点联系人日报", "contacts", 30, False),
    ("group-daily/cross_group_links.md", "商单信号雷达", "radar", 40, False),
    ("group-daily/group_daily_appendix.md", "群聊证据附录", "internal", 80, True),
    ("group-daily/group_daily_digest.md", "群聊机器初筛", "internal", 81, True),
    ("group-daily/group_selection_matrix.md", "全部群聊价值矩阵", "internal", 82, True),
    ("contact-daily/contact_daily_digest.md", "私信机器初筛", "internal", 82, True),
)


PAGE_SPECS: tuple[tuple[str, str, str, str, str, str, int], ...] = (
    ("overview", "index.md", "综合行动报告", "综合行动", "先看结论、优先行动、正在推进的商机和覆盖范围。", "●", 10),
    ("groups", "groups.md", "群聊日报", "群聊日报", "在话题、重点群聊和全部群聊筛选之间切换；建议级别只针对本时段。", "#", 20),
    ("contacts", "contacts.md", "重点联系人", "重点联系人", "查看品牌方、中间人和自媒体博主的待回复、等待与复联。", "◎", 30),
    ("radar", "radar.md", "商单信号雷达", "信号雷达", "每个标准化链接只出现一次，用于判断哪些品牌正在集中投放。", "↗", 40),
)

KIND_LABELS = {kind: label for kind, _, _, label, _, _, _ in PAGE_SPECS}


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "-", value.lower()).strip("-")
    return value or "section"


def _markdown_title(text: str, fallback: str) -> str:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if not match:
        return fallback
    value = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", match.group(1))
    return re.sub(r"[`*_]", "", value).strip() or fallback


def _source_rule(relative_path: str) -> tuple[str, str, int, bool] | None:
    for pattern, title, kind, priority, collapsed in SOURCE_RULES:
        if relative_path == pattern:
            return title, kind, priority, collapsed
    return None


MANIFEST_NAME = "report_manifest.json"
MANIFEST_PRIORITY = 55
KNOWN_SOURCE_NAMES = {pattern.rsplit("/", 1)[-1] for pattern, *_ in SOURCE_RULES}


def load_report_manifest(root: Path) -> dict[str, tuple[str, str]]:
    """Read the optional manifest that assigns extra final reports to a page.

    Format: {"sources": [{"path": "services_review.md", "page": "contacts", "title": "..."}]}.
    A topical report only reaches the HTML when the editor names its page here;
    machine drafts in the same directory stay out of the reading view.
    """
    path = root / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{MANIFEST_NAME} 无法读取：{exc}") from exc
    entries = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{MANIFEST_NAME} 需要 sources 列表")
    routes = {route for route, *_ in PAGE_SPECS}
    manifest: dict[str, tuple[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{MANIFEST_NAME} 的每一项都应是对象")
        relative_path = str(entry.get("path") or "").strip()
        page = str(entry.get("page") or "").strip()
        target = (root / relative_path).resolve()
        if not relative_path or not target.is_relative_to(root) or not target.is_file() or target.suffix != ".md":
            raise ValueError(f"{MANIFEST_NAME} 的 path 无效：{relative_path or '空'}")
        if page not in routes:
            raise ValueError(f"{MANIFEST_NAME} 的 page 无效：{page or '空'}；可选 {'、'.join(sorted(routes))}")
        manifest[target.relative_to(root).as_posix()] = (page, str(entry.get("title") or "").strip())
    return manifest


def unmapped_report_sources(sources: list[ReportSource]) -> list[ReportSource]:
    """Markdown files that no page shows and that were not declared internal."""
    return [source for source in sources if source.kind == "other"]


def discover_report_sources(report_dir: Path) -> list[ReportSource]:
    root = report_dir.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"找不到报告目录：{root}")

    used_ids: set[str] = set()
    sources: list[ReportSource] = []
    manifest = load_report_manifest(root)
    dual_group_report = any(
        (root / "group-daily" / name).exists()
        for name in ("group_daily_topics.md", "group_daily_groups.md")
    )
    for path in sorted(root.rglob("*.md")):
        relative_path = path.relative_to(root).as_posix()
        if path.name in GENERATED_NAMES or path.name.startswith(".") or relative_path.startswith(f"{MARKDOWN_SITE_DIR}/"):
            continue
        if dual_group_report and relative_path == "group-daily/group_daily_brief.md":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rule = _source_rule(relative_path)
        if rule:
            title, kind, priority, collapsed = rule
        elif relative_path in manifest:
            kind, manifest_title = manifest[relative_path]
            title = manifest_title or _markdown_title(text, path.stem.replace("_", " "))
            priority, collapsed = MANIFEST_PRIORITY, False
        else:
            title = _markdown_title(text, path.stem.replace("_", " "))
            # A copy of a known machine file outside its usual folder is still internal.
            kind = "internal" if path.name in KNOWN_SOURCE_NAMES else "other"
            priority, collapsed = 60, True
        base = _slug(relative_path.removesuffix(".md"))
        source_id = base
        suffix = 2
        while source_id in used_ids:
            source_id = f"{base}-{suffix}"
            suffix += 1
        used_ids.add(source_id)
        sources.append(ReportSource(path, relative_path, source_id, title, kind, priority, collapsed))
    return sorted(sources, key=lambda item: (item.priority, item.relative_path))


def build_report_pages(sources: list[ReportSource]) -> list[ReportPage]:
    grouped: dict[str, list[ReportSource]] = {}
    for source in sources:
        grouped.setdefault(source.kind, []).append(source)
    pages: list[ReportPage] = []
    for kind, filename, title, nav_label, description, glyph, priority in PAGE_SPECS:
        page_sources = tuple(grouped.get(kind, []))
        if page_sources:
            pages.append(ReportPage(kind, filename, title, nav_label, description, glyph, priority, page_sources))
    return pages


def _normalize_details_markdown(text: str) -> str:
    """Turn leaked HTML disclosure markup into readable Markdown headings."""
    text = re.sub(r"(?is)<details(?:\s+open)?>\s*", "", text)
    text = re.sub(r"(?is)\s*</details>", "", text)

    def replace_summary(match: re.Match[str]) -> str:
        value = re.sub(r"(?is)</?strong>", "", match.group(1))
        value = re.sub(r"\s+", " ", value).strip()
        if "｜" in value:
            title, note = (part.strip() for part in value.split("｜", 1))
            return f"### {title}\n\n> {note}"
        return f"### {value}"

    text = re.sub(r"(?is)<summary>(.*?)</summary>", replace_summary, text)
    text = re.sub(r"(?ms)^##\s+(?:详细报告|报告路径)\s*\n.*?(?=^##\s+|\Z)", "", text)
    return text.strip()


def _strip_first_h1(text: str) -> str:
    return re.sub(r"(?m)^#\s+.+?\s*\n+", "", text, count=1).strip()


def build_combined_markdown(sources: list[ReportSource], *, title: str, generated_at: str) -> str:
    """Compatibility helper; the flagship renderer no longer writes this long form."""
    parts = [f"# {title}", "", f"> 综合 {len(sources)} 份本轮 Markdown 报告｜生成时间：{generated_at}", ""]
    for source in sources:
        text = source.path.read_text(encoding="utf-8", errors="replace")
        parts.extend([f"## {source.title}", "", f"> 来源文件：`{source.relative_path}`", "", _strip_first_h1(text), ""])
    return "\n".join(parts).rstrip() + "\n"


def _markdown_navigation(pages: list[ReportPage], current_route: str) -> str:
    links = []
    for page in pages:
        label = f"**{page.nav_label}**" if page.route == current_route else page.nav_label
        links.append(f"[{label}]({page.filename})")
    return " · ".join(links)


def _rewrite_markdown_links(
    text: str,
    *,
    source: ReportSource,
    source_pages: dict[Path, ReportPage],
    markdown_dir: Path,
) -> str:
    def replace(match: re.Match[str]) -> str:
        marker, label, href = match.group(1), match.group(2), match.group(3)
        try:
            parsed = urlsplit(href)
        except ValueError:
            return match.group(0)
        if parsed.scheme in {"http", "https", "mailto"} or href.startswith("#"):
            return match.group(0)
        candidate = (source.path.parent / unquote(parsed.path)).resolve()
        target_page = source_pages.get(candidate)
        if target_page:
            target = target_page.filename + (f"#{parsed.fragment}" if parsed.fragment else "")
            return f"{marker}[{label}]({target})"
        if candidate.name in GENERATED_NAMES:
            return f"{marker}[{label}](index.md)"
        if candidate.name == "wechat_daily_report.html":
            return f"{marker}[{label}](../wechat_daily_report.html#/overview)"
        if parsed.path:
            relative = Path(os.path.relpath(candidate, markdown_dir)).as_posix()
            target = relative + (f"#{parsed.fragment}" if parsed.fragment else "")
            return f"{marker}[{label}]({target})"
        return match.group(0)

    return re.sub(r"(!?)\[([^]]*)\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)", replace, text)


def _build_page_markdown(
    page: ReportPage,
    *,
    pages: list[ReportPage],
    source_pages: dict[Path, ReportPage],
    markdown_dir: Path,
    generated_at: str,
) -> str:
    parts = [
        f"# {page.title}", "", f"> {page.description}｜生成时间：{generated_at}", "",
        _markdown_navigation(pages, page.route), "", "---", "",
    ]
    multiple = len(page.sources) > 1
    for source in page.sources:
        text = _normalize_details_markdown(_rewrite_markdown_links(
            source.path.read_text(encoding="utf-8", errors="replace"),
            source=source,
            source_pages=source_pages,
            markdown_dir=markdown_dir,
        ))
        if multiple:
            parts.extend([f"## {source.title}", "", f"> 来源：`{source.relative_path}`", ""])
        parts.extend([_strip_first_h1(text), ""])
    parts.extend(["---", "", f"[返回报告入口](../wechat_daily_full.md) · [打开交互版](../wechat_daily_report.html#/{page.route})", ""])
    return "\n".join(parts).rstrip() + "\n"


def write_markdown_site(
    report_dir: Path,
    pages: list[ReportPage],
    *,
    portal_path: Path,
    title: str,
    generated_at: str,
) -> dict[str, str]:
    root = report_dir.expanduser().resolve()
    markdown_dir = root / MARKDOWN_SITE_DIR
    markdown_dir.mkdir(parents=True, exist_ok=True)
    expected_files = {page.filename for page in pages}
    if any(page.route == "groups" and len(page.sources) > 1 for page in pages):
        expected_files.update({"group-topics.md", "key-groups.md"})
    for stale_path in markdown_dir.glob("*.md"):
        if stale_path.name not in expected_files:
            stale_path.unlink()
    source_pages = {source.path.resolve(): page for page in pages for source in page.sources}
    markdown_by_route: dict[str, str] = {}
    for page in pages:
        if page.route == "groups" and len(page.sources) > 1:
            group_links: list[str] = []
            for source in page.sources:
                filename = "group-topics.md" if source.relative_path.endswith("group_daily_topics.md") else "key-groups.md"
                source_page = ReportPage(page.route, filename, source.title, source.title, page.description, page.glyph, page.priority, (source,))
                source_content = _build_page_markdown(
                    source_page,
                    pages=pages,
                    source_pages=source_pages,
                    markdown_dir=markdown_dir,
                    generated_at=generated_at,
                )
                (markdown_dir / filename).write_text(source_content, encoding="utf-8")
                group_links.append(f"- [{source.title}]({filename})")
            matrix_path = root / "group-daily" / "group_selection_matrix.md"
            if matrix_path.exists():
                group_links.append("- [全部群聊价值矩阵](../group-daily/group_selection_matrix.md)")
            content = "\n".join([
                f"# {page.title}", "", f"> {page.description}", "", _markdown_navigation(pages, page.route), "",
                "## 选择阅读方式", "", *group_links, "",
                "话题日报用于跨群理解同一事件；重点群聊用于回到具体群查看上下文；全部群聊价值矩阵用于逐群调整关注级别。", "",
                "---", "", "[返回报告入口](../wechat_daily_full.md) · [打开交互版](../wechat_daily_report.html#/groups)", "",
            ])
        else:
            content = _build_page_markdown(
                page,
                pages=pages,
                source_pages=source_pages,
                markdown_dir=markdown_dir,
                generated_at=generated_at,
            )
        (markdown_dir / page.filename).write_text(content, encoding="utf-8")
        markdown_by_route[page.route] = content

    lines = [
        f"# {title}", "",
        f"> 生成时间：{generated_at}｜{len(pages)} 个阅读分区｜完整内容已按主题拆分，不再纵向拼成一篇长文。",
        "", "## 阅读入口", "", "- [打开旗舰版交互日报](wechat_daily_report.html)",
    ]
    lines.extend(f"- [{page.title}]({MARKDOWN_SITE_DIR}/{page.filename})：{page.description}" for page in pages)
    lines.extend([
        "", "## 阅读说明", "",
        "- 交互 HTML 只保留综合行动、群聊日报、重点联系人和商单信号雷达四个入口。",
        "- 群聊日报可切换话题视角和重点群视角；Markdown 也分成两份，不再纵向堆叠。",
        "- 证据附录和机器初筛仍保留在本地运行目录，但不作为阅读入口。",
        "- 所有微信读取均为本地只读，报告不会自动发送消息。", "",
    ])
    portal_path.parent.mkdir(parents=True, exist_ok=True)
    portal_path.write_text("\n".join(lines), encoding="utf-8")
    return markdown_by_route


def _pandoc_fragment(source: ReportSource, pandoc: str) -> str:
    command = [pandoc, "--sandbox", "--from=gfm-raw_html", "--to=html5", f"--id-prefix={source.source_id}-"]
    normalized = _normalize_details_markdown(source.path.read_text(encoding="utf-8", errors="replace"))
    result = subprocess.run(command, input=normalized, text=True, capture_output=True, timeout=60)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Pandoc 转换失败").strip()
        raise RuntimeError(f"{source.relative_path}: {detail}")
    fragment = re.sub(r"(?s)^.*?<h1[^>]*>.*?</h1>", "", result.stdout, count=1).strip()
    return sanitize_fragment(fragment, id_prefix=f"{source.source_id}-", allow_parent=True)


def _rewrite_html_links(fragment: str, *, source: ReportSource, source_pages: dict[Path, ReportPage]) -> str:
    def replace(match: re.Match[str]) -> str:
        before, href, after = match.group(1), match.group(2), match.group(3)
        try:
            parsed = urlsplit(href)
        except ValueError:
            return match.group(0)
        if parsed.scheme in {"http", "https"}:
            return f'<a{before}href="{href}"{after} target="_blank" class="original-link">'
        if parsed.scheme:
            return match.group(0)
        if href.startswith("#"):
            return f'<a{before}href="#/{source.kind}/{source.source_id}-{parsed.fragment}"{after} class="internal-link">'
        candidate = (source.path.parent / unquote(parsed.path)).resolve()
        target_page = source_pages.get(candidate)
        if target_page:
            target_source = next(item for item in target_page.sources if item.path.resolve() == candidate)
            anchor = f"{target_source.source_id}-{parsed.fragment}" if parsed.fragment else f"source-{target_source.source_id}"
            return f'<a{before}href="#/{target_page.route}/{anchor}"{after} class="internal-link">'
        if candidate.name in GENERATED_NAMES or candidate.name == "wechat_daily_report.html":
            return f'<a{before}href="#/overview"{after} class="internal-link">'
        return f'<a{before}{after}>'

    return re.sub(r'<a([^>]*?)href="([^"]+)"([^>]*)>', replace, fragment)


def _external_urls(text: str) -> set[str]:
    urls = set(re.findall(r"https?://[^\s<>()\]\[\"']+", text))
    return {url.rstrip(".,;:!?，。；：！？") for url in urls}


def _reading_minutes(text: str) -> int:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    latin = len(re.findall(r"\b[A-Za-z][A-Za-z0-9'-]*\b", text))
    return max(1, round(chinese / 500 + latin / 250))


def _heading_count(page: ReportPage) -> int:
    return sum(len(re.findall(r"(?m)^#{2,4}\s+", source.path.read_text(encoding="utf-8", errors="replace"))) for source in page.sources)


def _wrap_key_group_sections(fragment: str) -> str:
    """Make each key-group heading an HTML disclosure without touching Markdown."""
    level = 2 if re.search(r"<h2\b", fragment, re.I) else 3
    heading_pattern = re.compile(rf'<h{level}(?:\s+id="([^"]+)")?[^>]*>(.*?)</h{level}>', re.I | re.S)
    matches = list(heading_pattern.finditer(fragment))
    if not matches:
        return fragment
    parts = [fragment[:matches[0].start()]]
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(fragment)
        anchor = match.group(1) or f"key-group-{index + 1}"
        title = match.group(2).strip()
        body = fragment[match.end():end].strip()
        open_attribute = ""
        parts.append(
            f'<details class="key-group-card" id="{escape(anchor)}"{open_attribute}>'
            f'<summary><span class="key-group-title">{title}</span><span class="key-group-hint">点击展开</span></summary>'
            f'<div class="key-group-body">{body}</div></details>'
        )
    return "".join(parts)


def _load_group_matrix(report_dir: Path) -> dict[str, object] | None:
    path = report_dir / "group-daily" / "group_selection_matrix.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    groups = payload.get("groups") if isinstance(payload, dict) else None
    return payload if isinstance(groups, list) else None


def _render_group_selector(payload: dict[str, object]) -> str:
    raw_rows = payload.get("groups")
    rows = raw_rows if isinstance(raw_rows, list) else []
    level_options = ("重点", "雷达观察", "关注", "低优先级", "排除候选")
    raw_dimensions = payload.get("dimensions")
    dimensions = tuple(
        str(item).strip() for item in raw_dimensions
        if str(item).strip()
    ) if isinstance(raw_dimensions, list) else GROUP_VALUE_DIMENSIONS
    row_html: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        group = str(row.get("群聊") or "未命名群聊")
        level = str(row.get("建议关注级别") or "关注")
        if level not in level_options:
            level = "关注"
        tags = [label for label in dimensions if bool(row.get(label))]
        rendered_tags: list[str] = []
        for tag in tags:
            count = int(row.get(f"{tag}消息数") or 0)
            suffix = f" · {count}" if count else ""
            rendered_tags.append(f'<span class="matrix-tag">{escape(tag)}{suffix}</span>')
        tag_html = "".join(rendered_tags) or '<span class="matrix-tag muted-tag">无目标标签</span>'
        options = "".join(
            f'<option value="{escape(option)}"{" selected" if option == level else ""}>{escape(option)}</option>'
            for option in level_options
        )
        row_html.append(
            f'<article class="matrix-row" data-group-row data-group-name="{escape(group.lower())}" '
            f'data-level="{escape(level)}" data-tags="{escape(",".join(tags))}">'
            '<div class="matrix-group"><details class="matrix-detail">'
            f'<summary><strong>{escape(group)}</strong><span class="matrix-expand-hint">展开</span></summary>'
            f'<div class="matrix-detail-body"><p><b>群聊类型：</b>{escape(str(row.get("群聊类型") or "一般信息"))}</p>'
            f'<p><b>主要主题：</b>{escape(str(row.get("主要主题") or "未识别"))}</p>'
            f'<p><b>判断：</b>{escape(str(row.get("判断依据") or ""))}</p></div></details></div>'
            f'<div class="matrix-activity"><strong>{escape(str(row.get("活跃度") or "低"))}</strong>'
            f'<span>{escape(str(row.get("消息数") or 0))} 条 / 有效 {escape(str(row.get("有效讨论数") or 0))}</span></div>'
            f'<div class="matrix-tags">{tag_html}</div>'
            f'<div class="matrix-reason">{escape(str(row.get("判断依据") or ""))}</div>'
            f'<label class="matrix-choice"><span class="sr-only">设置 {escape(group)} 的关注级别</span>'
            f'<select data-group-priority data-suggested="{escape(level)}" data-group="{escape(group)}">{options}</select></label>'
            '</article>'
        )
    basis = escape(str(payload.get("basis") or "建议级别只针对本时段，不等于永久排除。"))
    return (
        '<section class="source-document group-selector" id="group-selection-matrix" data-group-view="selector">'
        '<header class="source-title"><p>群聊管理</p><h2>全部群聊筛选</h2>'
        f'<span class="selector-note">{basis}</span></header>'
        '<div class="matrix-toolbar">'
        '<label class="matrix-search"><span class="sr-only">搜索群名</span><input id="group-matrix-search" type="search" placeholder="搜索群名"></label>'
        '<label><span>关注级别</span><select id="group-level-filter"><option value="all">全部</option><option value="重点">重点</option><option value="雷达观察">雷达观察</option><option value="关注">关注</option><option value="低优先级">低优先级</option><option value="排除候选">排除候选</option></select></label>'
        '<label><span>目标方向</span><select id="group-tag-filter"><option value="all">全部</option>'
        + "".join(f'<option value="{escape(tag)}">{escape(tag)}</option>' for tag in dimensions)
        + '</select></label><button class="matrix-export" id="group-selection-export" type="button">导出选择</button></div>'
        '<p class="matrix-count" id="group-matrix-count" aria-live="polite"></p>'
        '<div class="matrix-head"><span>群聊（点击展开）</span><span>活跃度</span><span>相关方向</span><span>本时段判断</span><span>我的选择</span></div>'
        f'<div class="matrix-list">{"".join(row_html)}</div><p class="matrix-empty" id="group-matrix-empty" hidden>没有符合当前筛选条件的群聊。</p>'
        '</section>'
    )


def _render_page(
    page: ReportPage,
    fragments: dict[str, str],
    group_matrix: dict[str, object] | None = None,
) -> str:
    sections: list[str] = []
    multiple = len(page.sources) > 1
    for source in page.sources:
        group_view = ""
        if source.relative_path.endswith("group_daily_topics.md"):
            group_view = "topics"
        elif source.relative_path.endswith("group_daily_groups.md"):
            group_view = "groups"
        elif page.route == "groups":
            group_view = "topics"
        heading = (
            f'<header class="source-title"><p>{escape(KIND_LABELS.get(source.kind, "报告"))}</p><h2>{escape(source.title)}</h2><code>{escape(source.relative_path)}</code></header>'
            if multiple else f'<p class="source-origin">完整来源 · {escape(source.relative_path)}</p>'
        )
        fragment = fragments[source.source_id]
        if source.relative_path.endswith("group_daily_groups.md"):
            fragment = _wrap_key_group_sections(fragment)
        sections.append(
            f'<section class="source-document" id="source-{escape(source.source_id)}" data-source="{escape(source.relative_path)}"'
            f'{f" data-group-view={json.dumps(group_view)}" if group_view else ""}>{heading}'
            f'<div class="markdown-body">{fragment}</div></section>'
        )
    if page.route == "groups" and group_matrix:
        sections.append(_render_group_selector(group_matrix))
    switcher = ""
    if page.route == "groups" and (len(page.sources) > 1 or group_matrix):
        switcher = (
            '<div class="view-switcher" role="tablist" aria-label="群聊日报阅读方式">'
            + ('<button class="view-button active" type="button" data-group-view-button="topics" role="tab" aria-selected="true">话题日报</button>' if any(not s.relative_path.endswith('group_daily_groups.md') for s in page.sources) else '')
            + ('<button class="view-button" type="button" data-group-view-button="groups" role="tab" aria-selected="false">重点群聊</button>' if any(s.relative_path.endswith('group_daily_groups.md') for s in page.sources) else '')
            + ('<button class="view-button" type="button" data-group-view-button="selector" role="tab" aria-selected="false">群聊筛选</button>' if group_matrix else '')
            + '</div>'
        )
    return (
        f'<section class="report-page" data-route="{escape(page.route)}" hidden>'
        f'<header class="page-head"><div><p class="overline">WeChat intelligence report</p><h1>{escape(page.title)}</h1><p>{escape(page.description)}</p></div>'
        f'<div class="page-stat"><strong>{_heading_count(page)}</strong><span>内容节点</span></div></header>'
        f'{switcher}<div class="page-content">{"".join(sections)}</div></section>'
    )


def _replace_tokens(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(f"__{key}__", value)
    return template


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="description" content="微信个人情报库综合交互日报">
  <title>__PAGE_TITLE__</title>
  <style>
    :root { color-scheme:light; --chrome:#202b32; --chrome-2:#304149; --canvas:#eef2f2; --paper:#fff; --paper-2:#f5f8f8; --ink:#17252a; --muted:#51636a; --faint:#617178; --line:#d9e2e3; --line-strong:#bfcfd1; --green:#087d68; --green-soft:#e1f2ed; --blue:#316ca6; --coral:#be5547; --amber:#94620d; --shadow:0 8px 26px rgba(15,37,41,.06); }
    html[data-theme="dark"] { color-scheme:dark; --chrome:#10191e; --chrome-2:#203139; --canvas:#141e23; --paper:#1e2a30; --paper-2:#26343b; --ink:#eff6f5; --muted:#b6c8cc; --faint:#a2b5b9; --line:#34474e; --line-strong:#50656c; --green:#74d8ba; --green-soft:#25463e; --blue:#9cc5f3; --coral:#f39887; --amber:#f0c77b; --shadow:0 8px 28px rgba(0,0,0,.2); }
    html[data-style="brief"] { --chrome:#f8f8f4; --chrome-2:#e9eee9; --canvas:#f8f8f4; --paper:#fff; --paper-2:#f1f4f1; --ink:#1d2928; --muted:#506361; --faint:#5e716e; --line:#dce5df; --line-strong:#bccdc4; --green:#176e55; --green-soft:#e9f3eb; --blue:#285f8f; --coral:#a84e41; --amber:#8b6013; --shadow:none; }
    html[data-style="brief"][data-theme="dark"] { --chrome:#1b2422; --chrome-2:#2b3b36; --canvas:#1b2422; --paper:#222e2a; --paper-2:#293832; --ink:#f1f4ed; --muted:#bacbc0; --faint:#a5b9ae; --line:#3b5045; --line-strong:#587163; --green:#8ad7ad; --green-soft:#2e4937; --blue:#aaccea; --coral:#ed9e8d; --amber:#e9c57b; --shadow:none; }
    html[data-style="ledger"] { --chrome:#20343a; --chrome-2:#2c4a51; --canvas:#f1f4f5; --paper:#fff; --paper-2:#f5f8f9; --ink:#19292f; --muted:#50646c; --faint:#62777e; --line:#dce5e8; --line-strong:#bfcfd4; --green:#097768; --green-soft:#e2f1ee; --blue:#326a9e; --coral:#b6544b; --amber:#91600e; --shadow:none; }
    html[data-style="ledger"][data-theme="dark"] { --chrome:#122329; --chrome-2:#254047; --canvas:#142227; --paper:#1d2e34; --paper-2:#273940; --ink:#eef5f4; --muted:#b8c9cc; --faint:#a4b8bc; --line:#374f56; --line-strong:#526a70; --green:#83d8c1; --green-soft:#25483e; --blue:#a4c9ed; --coral:#ee9b8d; --amber:#eac681; --shadow:none; }
    html:not([data-ready]) body { visibility:hidden; }
    * { box-sizing:border-box; }
    [hidden] { display:none!important; }
    :focus-visible { outline:2px solid var(--green); outline-offset:3px; }
    [id] { scroll-margin-top:142px; }
    html,body { margin:0; min-height:100%; }
    html { scroll-behavior:smooth; }
    body { background:var(--canvas); color:var(--ink); font:14px/1.7 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; letter-spacing:0; }
    button,input { font:inherit; letter-spacing:0; } button { cursor:pointer; } a { color:var(--green); text-underline-offset:3px; overflow-wrap:anywhere; }
    .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
    .app { min-height:100vh; }
    .sidebar { position:fixed; inset:0 0 auto 0; z-index:30; height:64px; display:flex; align-items:stretch; padding:0 28px; background:var(--chrome); color:#c8ced6; }
    .brand { min-width:250px; display:flex; align-items:center; gap:10px; color:#fff; font-weight:800; }
    .brand-mark { width:26px; height:26px; display:grid; place-items:center; border-radius:5px; background:#75d9c0; color:#10241f; font-size:12px; font-weight:900; }
    .workspace-switch,.nav-label,.sidebar-foot { display:none; }
    .nav-item { display:flex; align-items:center; gap:7px; min-height:64px; padding:0 17px; border-bottom:3px solid transparent; color:#c8ced6; text-decoration:none; font-size:13px; }
    .nav-item:hover { color:#fff; } .nav-item.active { border-color:#75d9c0; background:var(--chrome-2); color:#fff; }
    .nav-glyph { display:none; }
    .nav-count { display:none; }
    .fresh-dot { display:inline-block; width:6px; height:6px; margin-right:6px; border-radius:50%; background:#72d29f; }
    .workspace { min-width:0; padding-top:64px; }
    .location-bar { position:sticky; top:64px; z-index:25; height:54px; display:flex; align-items:center; gap:13px; padding:0 30px; border-bottom:1px solid var(--line-strong); background:color-mix(in srgb,var(--paper-2) 94%,transparent); backdrop-filter:blur(18px); }
    .crumbs { min-width:210px; color:var(--muted); font-size:12px; } .crumbs strong { color:var(--ink); }
    .search-wrap { position:relative; width:min(470px,42vw); margin:0 auto; }
    .search-input { width:100%; height:34px; padding:0 58px 0 11px; border:1px solid var(--line-strong); border-radius:6px; background:color-mix(in srgb,var(--paper) 88%,transparent); color:var(--ink); outline:none; }
    .search-input:focus { border-color:var(--green); box-shadow:0 0 0 3px color-mix(in srgb,var(--green) 15%,transparent); }
    .key { position:absolute; right:8px; top:6px; padding:1px 5px; border:1px solid var(--line); border-radius:4px; color:var(--faint); font-size:11px; }
    .toolbar { display:flex; gap:7px; flex:0 0 auto; } .icon-button { min-width:32px; height:32px; padding:0 10px; border:1px solid var(--line-strong); border-radius:6px; background:var(--paper); color:var(--muted); font-size:12px; white-space:nowrap; }
    .icon-button:hover { border-color:var(--green); color:var(--green); }
    .mobile-routes { display:none; }
    .report-page { padding:26px 30px 64px; } .report-page[hidden] { display:none!important; }
    .page-head { max-width:1080px; margin:0 auto 25px; display:flex; align-items:end; justify-content:space-between; gap:24px; padding-bottom:22px; border-bottom:1px solid var(--line-strong); }
    .overline { margin:0 0 5px; color:var(--green); font-size:12px; font-weight:800; text-transform:uppercase; }
    .page-head h1 { margin:0; font-size:30px; line-height:1.24; } .page-head p:not(.overline) { margin:7px 0 0; color:var(--muted); font-size:13px; }
    .page-stat { min-width:74px; padding-left:13px; border-left:2px solid var(--coral); } .page-stat strong { display:block; font-size:18px; line-height:1.2; } .page-stat span { color:var(--faint); font-size:12px; }
    .page-content { max-width:1080px; margin:0 auto; display:grid; grid-template-columns:minmax(0,1fr) 176px; align-items:start; gap:24px; }
    .source-document { grid-column:1; min-width:0; padding:8px 0 28px; border:0; background:transparent; box-shadow:none; }
    .source-document+.source-document { margin-top:16px; }
    .page-outline { grid-column:2; grid-row:1; position:sticky; top:142px; max-height:calc(100vh - 166px); overflow:auto; padding-left:16px; border-left:1px solid var(--line); font-size:12px; }
    .page-outline a { display:block; margin:0 0 10px; color:var(--muted); text-decoration:none; line-height:1.5; }
    .page-outline a:hover { color:var(--green); }
    .report-window { max-width:1080px; margin:0 auto; padding:16px 30px 0; color:var(--muted); font-size:12px; }
    .matrix-count { color:var(--muted); font-size:12px; }
    .target-flash { animation:target-flash 1.5s ease-out; }
    @keyframes target-flash { from { background:var(--green-soft); } to { background:transparent; } }
    .source-document[data-group-view][hidden] { display:none!important; }
    .view-switcher { width:100%; max-width:1080px; margin:0 auto 16px; display:flex; }
    .view-button { min-width:132px; padding:8px 16px; border:0; border-right:1px solid var(--line); background:transparent; color:var(--muted); }
    .view-button:last-child { border-right:0; } .view-button.active { background:var(--chrome); color:#fff; font-weight:760; }
    .source-origin,.source-title code { display:none!important; }
    .group-selector { grid-column:1/-1; }
    @media print { .page-outline,.report-window { display:none!important; } .page-content { display:block; } }
    .source-title { margin-bottom:20px; } .source-title p { margin:0 0 4px; color:var(--coral); font-size:12px; font-weight:800; text-transform:uppercase; } .source-title h2 { margin:0; font-size:22px; } .source-title code { display:inline-block; margin-top:5px; color:var(--faint); font-size:12px; }
    .markdown-body { max-width:860px; color:var(--ink); } .markdown-body h2 { margin:32px 0 12px; padding-top:2px; font-size:20px; line-height:1.35; } .markdown-body h2:first-child { margin-top:0; }
    .markdown-body h3 { margin:25px 0 9px; font-size:16px; } .markdown-body h4 { margin:20px 0 7px; font-size:14px; } .markdown-body p { margin:10px 0; }
    .markdown-body ul,.markdown-body ol { padding-left:22px; } .markdown-body li { margin:7px 0; }
    .markdown-body>ul>li,.markdown-body>ol>li { margin:8px 0; padding:6px 0; }
    .markdown-body blockquote { margin:15px 0; padding:11px 14px; border-left:3px solid var(--green); background:var(--green-soft); color:var(--muted); } .markdown-body blockquote p { margin:0; }
    .markdown-body code { padding:2px 5px; border-radius:3px; background:var(--paper-2); color:var(--coral); font-family:"SFMono-Regular",Consolas,monospace; font-size:.9em; }
    .markdown-body pre { overflow:auto; padding:15px; border:1px solid var(--line); border-radius:6px; background:#171a17; color:#eef0ea; } .markdown-body pre code { padding:0; background:transparent; color:inherit; }
    .markdown-body table { width:100%; display:block; overflow-x:auto; border-collapse:collapse; } .markdown-body th,.markdown-body td { padding:8px 10px; border:1px solid var(--line); text-align:left; min-width:100px; white-space:normal; overflow-wrap:anywhere; } .markdown-body th { background:var(--paper-2); }
    .markdown-body img { max-width:100%; height:auto; }
    .original-link::after { content:" ↗"; font-size:.78em; }
    .key-group-card { margin:0; border-top:1px solid var(--line); background:transparent; }
    .key-group-card:last-child { border-bottom:1px solid var(--line); }
    .key-group-card>summary { display:flex; align-items:center; justify-content:space-between; gap:18px; padding:17px 2px; list-style:none; cursor:pointer; }
    .key-group-card>summary::-webkit-details-marker { display:none; }
    .key-group-card>summary::before { content:"+"; width:22px; height:22px; flex:0 0 22px; display:grid; place-items:center; border:1px solid var(--line-strong); border-radius:4px; color:var(--muted); font-weight:800; }
    .key-group-card[open]>summary::before { content:"−"; color:var(--green); border-color:var(--green); }
    .key-group-title { order:2; flex:1; min-width:0; overflow-wrap:anywhere; font-size:16px; font-weight:700; }
    .key-group-hint { order:3; color:var(--faint); font-size:10px; }
    .key-group-card[open] .key-group-hint { color:var(--green); }
    .key-group-body { padding:0 34px 20px; }
    .group-selector { padding-bottom:20px; }
    .selector-note { display:block; max-width:860px; color:var(--muted); font-size:11px; }
    .matrix-toolbar { display:flex; align-items:end; gap:9px; margin:0 0 14px; padding:12px; border:1px solid var(--line); background:var(--paper-2); }
    .matrix-toolbar label { display:grid; gap:4px; color:var(--faint); font-size:9px; font-weight:700; }
    .matrix-toolbar input,.matrix-toolbar select,.matrix-choice select { height:34px; border:1px solid var(--line-strong); border-radius:5px; background:var(--paper); color:var(--ink); padding:0 9px; }
    .matrix-search { flex:1; } .matrix-search input { width:100%; }
    .matrix-export { height:34px; padding:0 12px; border:1px solid var(--green); border-radius:5px; background:var(--green); color:#fff; font-weight:750; }
    .matrix-head,.matrix-row { display:grid; grid-template-columns:minmax(230px,1.35fr) 105px minmax(230px,1.25fr) minmax(210px,1.15fr) 122px; gap:14px; align-items:center; }
    .matrix-head { padding:8px 12px; border-bottom:1px solid var(--line-strong); color:var(--faint); font-size:9px; font-weight:800; }
    .matrix-row { padding:12px; border-bottom:1px solid var(--line); }
    .matrix-row[hidden] { display:none!important; }
    .matrix-row:hover { background:var(--paper-2); }
    .matrix-row[data-level="低优先级"],.matrix-row[data-level="排除候选"] { color:var(--muted); }
    .matrix-group { min-width:0; }
    .matrix-detail>summary { display:flex; align-items:center; gap:7px; list-style:none; cursor:pointer; }
    .matrix-detail>summary::-webkit-details-marker { display:none; }
    .matrix-detail strong { min-width:0; overflow-wrap:anywhere; }
    .matrix-expand-hint { flex:0 0 auto; color:var(--green); font-size:9px; font-weight:700; }
    .matrix-detail[open] .matrix-expand-hint { font-size:0; }
    .matrix-detail[open] .matrix-expand-hint::after { content:"收起"; font-size:9px; }
    .matrix-detail-body { margin-top:8px; padding:8px 10px; border-left:2px solid var(--green); background:var(--green-soft); color:var(--muted); font-size:10px; }
    .matrix-detail-body p { margin:3px 0; }
    .matrix-activity { display:grid; } .matrix-activity strong { font-size:12px; } .matrix-activity span { color:var(--faint); font-size:9px; }
    .matrix-tags { display:flex; flex-wrap:wrap; gap:4px; }
    .matrix-tag { padding:2px 6px; border:1px solid color-mix(in srgb,var(--green) 35%,var(--line)); border-radius:3px; background:var(--green-soft); color:var(--green); font-size:9px; font-weight:700; }
    .muted-tag { border-color:var(--line); background:var(--paper-2); color:var(--faint); }
    .matrix-reason { color:var(--muted); font-size:10px; line-height:1.55; }
    .matrix-choice select { width:100%; }
    .matrix-empty { padding:34px 12px; color:var(--muted); text-align:center; }
    .search-panel { position:fixed; top:124px; left:50%; transform:translateX(-50%); z-index:60; width:min(560px,calc(100vw - 32px)); max-height:min(620px,calc(100vh - 140px)); overflow:auto; border:1px solid var(--line-strong); border-radius:8px; background:var(--paper); box-shadow:0 24px 70px rgba(17,22,18,.2); }
    .search-panel[hidden] { display:none; } .search-head { padding:11px 13px; border-bottom:1px solid var(--line); color:var(--muted); font-size:10px; }
    .search-result { display:block; padding:11px 13px; border-bottom:1px solid var(--line); color:var(--ink); text-decoration:none; } .search-result:hover { background:var(--paper-2); }
    .search-result strong { display:block; font-size:11px; } .search-result p { margin:3px 0 0; color:var(--muted); font-size:10px; line-height:1.55; } .search-empty { padding:28px 14px; color:var(--muted); text-align:center; }
    .appearance-panel { position:fixed; top:124px; right:24px; z-index:65; width:min(350px,calc(100vw - 24px)); padding:18px; border:1px solid var(--line-strong); border-radius:8px; background:var(--paper); box-shadow:0 20px 48px rgba(15,37,41,.2); }
    .appearance-panel h2 { margin:0 0 12px; font-size:17px; }
    .appearance-panel fieldset { min-width:0; margin:0 0 14px; padding:0; border:0; }
    .appearance-panel fieldset:last-child { margin-bottom:0; }
    .appearance-panel legend { margin-bottom:8px; color:var(--muted); font-size:12px; font-weight:700; }
    .appearance-options { display:grid; gap:6px; }
    .appearance-choice { position:relative; display:block; padding:9px 11px; border:1px solid var(--line); border-radius:5px; cursor:pointer; }
    .appearance-choice:hover { border-color:var(--green); }
    .appearance-choice:has(input:checked) { border-color:var(--green); background:var(--green-soft); }
    .appearance-choice:has(input:focus-visible) { outline:2px solid var(--green); outline-offset:2px; }
    .appearance-choice input { position:absolute; opacity:0; }
    .appearance-choice strong { display:block; line-height:1.3; font-size:13px; }
    .appearance-choice small { color:var(--muted); font-size:12px; }
    .appearance-modes { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:6px; }
    .appearance-modes .appearance-choice { padding:8px 3px; text-align:center; }
    .appearance-modes .appearance-choice strong { font-size:12px; }
    .appearance-actions { display:none; gap:8px; padding-top:13px; border-top:1px solid var(--line); }
    .appearance-actions button { min-height:34px; border:1px solid var(--line-strong); border-radius:5px; background:var(--paper-2); color:var(--ink); font-size:12px; }
    html[data-style="radar"] .sidebar { inset:0 auto 0 0; width:208px; height:auto; display:flex; flex-direction:column; align-items:stretch; padding:20px 10px; }
    html[data-style="radar"] .brand { min-width:0; padding:0 10px 23px; font-size:13px; line-height:1.35; }
    html[data-style="radar"] .nav-item { min-height:41px; margin:2px 0; padding:0 12px; border:0; border-radius:5px; }
    html[data-style="radar"] .nav-item.active { box-shadow:inset 3px 0 0 #75d9c0; }
    html[data-style="radar"] .sidebar-foot { display:block; margin-top:auto; padding:12px 9px; border-top:1px solid #466067; color:#b5c6ca; font-size:11px; line-height:1.7; }
    html[data-style="radar"] .workspace { margin-left:208px; padding-top:0; }
    html[data-style="radar"] .location-bar { top:0; }
    html[data-style="radar"] .appearance-panel { top:60px; }
    html[data-style="radar"] .search-panel { top:60px; }
    html[data-style="brief"] .sidebar { border-bottom:1px solid var(--line-strong); }
    html[data-style="brief"] .brand,html[data-style="brief"] .nav-item { color:var(--ink); }
    html[data-style="brief"] .nav-item:hover,html[data-style="brief"] .nav-item.active { color:var(--green); }
    html[data-style="brief"] .nav-item.active { background:transparent; border-color:var(--green); }
    html[data-style="brief"] .brand-mark { background:var(--green); color:var(--paper); }
    html[data-style="brief"] .page-head,html[data-style="brief"] .page-content,html[data-style="brief"] .report-window { max-width:900px; }
    html[data-style="brief"] .page-content { grid-template-columns:minmax(0,1fr) 150px; gap:36px; }
    html[data-style="brief"] .markdown-body { max-width:720px; font-size:15px; line-height:1.85; }
    html[data-style="brief"] .page-head h1 { font-family:Georgia,"Songti SC","STSong",serif; font-size:36px; font-weight:700; }
    html[data-style="brief"] .markdown-body h2 { font-family:Georgia,"Songti SC","STSong",serif; font-size:23px; }
    html[data-style="brief"] .report-page { padding-top:36px; }
    html[data-style="ledger"] .sidebar { height:54px; }
    html[data-style="ledger"] .nav-item { min-height:54px; }
    html[data-style="ledger"] .workspace { padding-top:54px; }
    html[data-style="ledger"] .location-bar { top:54px; height:48px; }
    html[data-style="ledger"] .report-page { padding-top:16px; }
    html[data-style="ledger"] .page-head { margin-bottom:12px; padding-bottom:12px; }
    html[data-style="ledger"] .page-head h1 { font-size:25px; }
    html[data-style="ledger"] .markdown-body { line-height:1.55; }
    html[data-style="ledger"] .markdown-body li { margin:4px 0; }
    html[data-style="ledger"] .markdown-body>ul>li,html[data-style="ledger"] .markdown-body>ol>li { margin:4px 0; padding:4px 0; }
    html[data-style="ledger"] .markdown-body h2 { margin-top:22px; }
    html[data-style="ledger"] .markdown-body th,html[data-style="ledger"] .markdown-body td { padding:6px 8px; }
    html[data-style="ledger"] .appearance-panel { top:108px; }
    html[data-style="ledger"] .search-panel { top:108px; }
    .matrix-toolbar label,.matrix-head,.matrix-expand-hint,.matrix-activity span,.matrix-tag,.matrix-reason,.matrix-detail-body,.search-head,.search-result strong,.search-result p,.key-group-hint,.selector-note { font-size:12px; }
    @media (prefers-reduced-motion:reduce) { html { scroll-behavior:auto; } *,*::before,*::after { animation-duration:.01ms!important; transition-duration:.01ms!important; } }
    .bottom-nav { display:none; }
    @media print { html,html[data-style],html[data-style][data-theme] { color-scheme:light; --canvas:#fff; --paper:#fff; --paper-2:#fff; --ink:#111; --muted:#333; --faint:#444; --line:#ccc; --line-strong:#aaa; --green:#166b56; --green-soft:#fff; } .sidebar,.location-bar,.mobile-routes,.bottom-nav,.appearance-panel { display:none!important; } .app { display:block; } .workspace,html[data-style="radar"] .workspace,html[data-style="ledger"] .workspace { margin:0; padding:0; } .report-page { padding:0; } }
    @media (max-width:900px) {
      .app { display:block; } .sidebar,html[data-style="radar"] .sidebar { display:none; } .workspace,html[data-style="radar"] .workspace,html[data-style="ledger"] .workspace { margin-left:0; padding-top:0; padding-bottom:56px; } .location-bar,html[data-style="radar"] .location-bar,html[data-style="ledger"] .location-bar { top:0; height:50px; padding:0 13px; } .crumbs { min-width:0; flex:1; }
      .search-wrap { width:42px; margin:0; } .search-input { width:42px; padding:0; color:transparent; caret-color:transparent; } .search-input::placeholder { color:transparent; } .search-wrap::before { content:"搜索"; position:absolute; z-index:2; left:8px; top:8px; color:var(--muted); font-size:12px; pointer-events:none; }
      .search-wrap:focus-within { position:fixed; inset:8px 12px auto; z-index:70; width:auto; } .search-wrap:focus-within .search-input { width:100%; padding:0 12px; color:var(--ink); caret-color:auto; } .search-wrap:focus-within .search-input::placeholder { color:var(--faint); } .key { display:none; }
      .toolbar #download-button,.toolbar #print-button { display:none; } .mobile-routes { display:none; }
      .mobile-routes a { flex:0 0 auto; padding:5px 8px; border-bottom:2px solid transparent; color:var(--muted); text-decoration:none; font-size:10px; } .mobile-routes a.active { border-color:var(--green); color:var(--ink); font-weight:750; }
      .report-page { padding:20px 13px 38px; } .page-head { align-items:start; margin-bottom:18px; padding-bottom:16px; } .page-head h1 { font-size:24px; } .page-head p:not(.overline) { font-size:12px; } .page-stat { min-width:86px; }
      .page-content { display:block; } .page-outline { display:none; } .report-window { padding:12px 13px 0; }
      .source-document { padding:8px 0 22px; } .view-switcher { width:100%; } .view-button { flex:1; min-width:0; padding:8px; }
      .markdown-body { max-width:none; } .markdown-body h2 { font-size:18px; } .markdown-body>ul>li,.markdown-body>ol>li { padding:7px 9px; }
      .key-group-body { padding-left:0; padding-right:0; }
      .matrix-toolbar { align-items:stretch; flex-direction:column; } .matrix-toolbar label,.matrix-export { width:100%; }
      .matrix-head { display:none; } .matrix-row { grid-template-columns:1fr 106px; gap:9px 12px; }
      .matrix-group,.matrix-tags,.matrix-reason { grid-column:1/-1; } .matrix-activity { grid-column:1; } .matrix-choice { grid-column:2; grid-row:2; }
      .search-panel,html[data-style="radar"] .search-panel,html[data-style="ledger"] .search-panel { top:52px; left:12px; right:12px; width:auto; max-height:calc(100vh - 110px); transform:none; } .appearance-panel,html[data-style="radar"] .appearance-panel,html[data-style="ledger"] .appearance-panel { top:52px; right:8px; } .appearance-actions { display:grid; grid-template-columns:1fr 1fr; }
      .bottom-nav { position:fixed; inset:auto 0 0; z-index:40; height:54px; display:grid; grid-template-columns:repeat(__BOTTOM_COUNT__,1fr); border-top:1px solid var(--line-strong); background:color-mix(in srgb,var(--paper) 94%,transparent); backdrop-filter:blur(18px); }
      .bottom-nav a { display:grid; place-items:center; align-content:center; color:var(--muted); text-decoration:none; font-size:12px; } .bottom-nav a span { display:none; } .bottom-nav a.active { color:var(--green); font-weight:800; border-top:2px solid var(--green); }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">W</span><span>WeChat Intelligence</span></div>
      <div class="workspace-switch">Local workspace</div>
      <div class="nav-label">Report</div>__NAV_HTML__
      <div class="sidebar-foot"><span class="fresh-dot"></span>本地只读 · __GENERATED_AT__<br>__SOURCE_COUNT__ 份源报告 · __LINK_COUNT__ 个原链接 · 完整阅读约 __READING_MINUTES__ 分钟</div>
    </aside>
    <div class="workspace">
      <header class="location-bar">
        <div class="crumbs">Report / <strong id="current-title">今日总览</strong></div>
        <label class="search-wrap"><span class="sr-only">搜索完整报告</span><input class="search-input" id="search-input" type="search" placeholder="搜索联系人、群聊、品牌、项目或链接"><span class="key">⌘K</span></label>
        <div class="toolbar"><button class="icon-button" id="download-button" type="button" title="下载当前分区 Markdown">下载</button><button class="icon-button" id="print-button" type="button" title="打印当前分区">打印</button><button class="icon-button" id="appearance-button" type="button" aria-haspopup="dialog" aria-controls="appearance-panel" aria-expanded="false" title="调整报告外观">外观</button></div>
      </header>
      <nav class="mobile-routes" aria-label="移动端报告目录">__MOBILE_NAV_HTML__</nav>
      <p class="report-window">__REPORT_WINDOW__</p>
      <main>__PAGES_HTML__</main>
    </div>
  </div>
  <div class="search-panel" id="search-panel" hidden><div class="search-head" id="search-head">输入关键词搜索全部分区</div><div id="search-results"></div></div>
  <section class="appearance-panel" id="appearance-panel" role="dialog" aria-label="报告外观" hidden>
    <h2>报告外观</h2>
    <fieldset><legend>阅读风格</legend><div class="appearance-options">
      <label class="appearance-choice"><input type="radio" name="report-style" value="radar"><strong>信号雷达</strong><small>按优先级快速扫读，适合日常跟进</small></label>
      <label class="appearance-choice"><input type="radio" name="report-style" value="brief"><strong>编辑简报</strong><small>宽松的长文排版，适合沉浸阅读</small></label>
      <label class="appearance-choice"><input type="radio" name="report-style" value="ledger"><strong>台账</strong><small>紧凑的信息密度，适合逐项核对</small></label>
    </div></fieldset>
    <fieldset><legend>颜色模式</legend><div class="appearance-modes">
      <label class="appearance-choice"><input type="radio" name="report-mode" value="system"><strong>跟随系统</strong></label>
      <label class="appearance-choice"><input type="radio" name="report-mode" value="light"><strong>亮色</strong></label>
      <label class="appearance-choice"><input type="radio" name="report-mode" value="dark"><strong>暗色</strong></label>
    </div></fieldset>
    <div class="appearance-actions"><button id="appearance-download" type="button">下载当前分区</button><button id="appearance-print" type="button">打印报告</button></div>
  </section>
  <nav class="bottom-nav" aria-label="常用分区">__BOTTOM_NAV_HTML__</nav>
  <script>
    function readSetting(key,fallback=null){try{return localStorage.getItem(key)??fallback;}catch{return fallback;}}
    function writeSetting(key,value){try{localStorage.setItem(key,value);}catch{}}
    let savedAppearance={};try{savedAppearance=JSON.parse(readSetting('wechat-flagship-appearance','{}'))||{};}catch{}
    const oldTheme=readSetting('wechat-flagship-theme');
    const appearance={style:['radar','brief','ledger'].includes(savedAppearance.style)?savedAppearance.style:'radar',mode:['system','light','dark'].includes(savedAppearance.mode)?savedAppearance.mode:(['light','dark'].includes(oldTheme)?oldTheme:'system')};
    const systemTheme=window.matchMedia('(prefers-color-scheme: dark)');
    function applyAppearance(){document.documentElement.dataset.style=appearance.style;document.documentElement.dataset.theme=appearance.mode==='system'?(systemTheme.matches?'dark':'light'):appearance.mode;document.documentElement.dataset.ready='true';document.querySelectorAll('input[name="report-style"]').forEach(input=>{input.checked=input.value===appearance.style;});document.querySelectorAll('input[name="report-mode"]').forEach(input=>{input.checked=input.value===appearance.mode;});}
    applyAppearance();systemTheme.addEventListener?.('change',()=>{if(appearance.mode==='system')applyAppearance();});
    const appearanceButton=document.getElementById('appearance-button');const appearancePanel=document.getElementById('appearance-panel');
    function closeAppearance(){appearancePanel.hidden=true;appearanceButton.setAttribute('aria-expanded','false');}
    appearanceButton.addEventListener('click',()=>{document.getElementById('search-panel').hidden=true;appearancePanel.hidden=!appearancePanel.hidden;appearanceButton.setAttribute('aria-expanded',String(!appearancePanel.hidden));if(!appearancePanel.hidden)appearancePanel.querySelector('input:checked')?.focus();});
    appearancePanel.addEventListener('change',event=>{if(event.target.name==='report-style')appearance.style=event.target.value;if(event.target.name==='report-mode')appearance.mode=event.target.value;applyAppearance();writeSetting('wechat-flagship-appearance',JSON.stringify({v:1,...appearance}));});
    document.getElementById('appearance-download').addEventListener('click',()=>document.getElementById('download-button').click());
    document.getElementById('appearance-print').addEventListener('click',()=>document.getElementById('print-button').click());
    document.addEventListener('click',event=>{if(!appearancePanel.hidden&&!appearancePanel.contains(event.target)&&!appearanceButton.contains(event.target))closeAppearance();});
    document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!appearancePanel.hidden){closeAppearance();appearanceButton.focus();}});
    const pageMeta=__PAGE_META_JSON__; const markdownByRoute=__MARKDOWN_JSON__;
    const reportPages=[...document.querySelectorAll('.report-page')]; const routeLinks=[...document.querySelectorAll('[data-route-link]')]; const currentTitle=document.getElementById('current-title');
    const groupViewButtons=[...document.querySelectorAll('[data-group-view-button]')]; const groupViews=[...document.querySelectorAll('[data-group-view]')];
    const groupRows=[...document.querySelectorAll('[data-group-row]')]; const groupPrioritySelects=[...document.querySelectorAll('[data-group-priority]')];
    const searchInput=document.getElementById('search-input'); const searchPanel=document.getElementById('search-panel'); const searchHead=document.getElementById('search-head'); const searchResults=document.getElementById('search-results'); const validRoutes=new Set(pageMeta.map(page=>page.route));
    function parseRoute(){const raw=location.hash.startsWith('#/')?location.hash.slice(2):''; const [candidate,...anchorParts]=raw.split('/'); let anchor=anchorParts.join('/'); try{anchor=decodeURIComponent(anchor);}catch{} return {route:validRoutes.has(candidate)?candidate:pageMeta[0].route,anchor};}
    function revealTarget(target){const view=target.closest('[data-group-view]'); if(view)setGroupView(view.dataset.groupView); let parent=target; while(parent){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;} target.scrollIntoView({behavior:'smooth',block:'start'});target.classList.remove('target-flash');requestAnimationFrame(()=>target.classList.add('target-flash'));}
    function renderRoute(){const {route,anchor}=parseRoute(); const meta=pageMeta.find(page=>page.route===route); reportPages.forEach(page=>{page.hidden=page.dataset.route!==route;}); routeLinks.forEach(link=>{const active=link.dataset.routeLink===route;link.classList.toggle('active',active);if(active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');}); currentTitle.textContent=meta.title; document.title=meta.title+'｜'+pageMeta[0].reportTitle; if(!location.hash.startsWith('#/'))history.replaceState(null,'',`#/${route}`); requestAnimationFrame(()=>{if(anchor){const target=document.getElementById(anchor); if(target&&target.closest('[data-route]')?.dataset.route===route)revealTarget(target);}else{window.scrollTo(0,0);}updateOutline();});}
    function updateOutline(){reportPages.forEach(page=>{const outline=page.querySelector('.page-outline');if(!outline)return;outline.replaceChildren();page.querySelectorAll('.markdown-body h2,.markdown-body h3,.key-group-card').forEach(node=>{if(node.closest('[data-group-view]')?.hidden)return;if(!node.id)return;const link=document.createElement('a');link.href=`#/${page.dataset.route}/${encodeURIComponent(node.id)}`;link.textContent=node.matches('details')?node.querySelector('.key-group-title').textContent:node.textContent;outline.append(link);});outline.hidden=!outline.children.length;});}
    function setGroupView(view){const available=new Set(groupViews.map(section=>section.dataset.groupView)); const selected=available.has(view)?view:(available.has('topics')?'topics':[...available][0]); groupViewButtons.forEach(button=>{const active=button.dataset.groupViewButton===selected; button.classList.toggle('active',active); button.setAttribute('aria-selected',active?'true':'false');}); groupViews.forEach(section=>{section.hidden=section.dataset.groupView!==selected;}); if(selected)writeSetting('wechat-group-view',selected);updateOutline();}
    function savedGroupPriorities(){try{const value=JSON.parse(readSetting('wechat-group-priorities','{}'));return value&&typeof value==='object'&&!Array.isArray(value)?value:{};}catch{return {};}}
    function persistGroupPriorities(){const values=savedGroupPriorities(); groupPrioritySelects.forEach(select=>{if(select.value!==select.dataset.suggested)values[select.dataset.group]=select.value;else delete values[select.dataset.group];}); writeSetting('wechat-group-priorities',JSON.stringify(values));}
    function filterGroupRows(){const query=(document.getElementById('group-matrix-search')?.value||'').trim().toLowerCase(); const level=document.getElementById('group-level-filter')?.value||'all'; const tag=document.getElementById('group-tag-filter')?.value||'all'; let visible=0; groupRows.forEach(row=>{const select=row.querySelector('[data-group-priority]'); const currentLevel=select?.value||row.dataset.level; const match=(!query||row.dataset.groupName.includes(query))&&(level==='all'||currentLevel===level)&&(tag==='all'||row.dataset.tags.split(',').includes(tag)); row.hidden=!match; if(match)visible+=1;}); const empty=document.getElementById('group-matrix-empty'); if(empty)empty.hidden=visible!==0; const count=document.getElementById('group-matrix-count');if(count)count.textContent=`显示 ${visible} / ${groupRows.length} 个群`;}
    function initGroupSelector(){if(!groupRows.length)return; const saved=savedGroupPriorities(); groupPrioritySelects.forEach(select=>{if([...select.options].some(option=>option.value===saved[select.dataset.group]))select.value=saved[select.dataset.group]; const row=select.closest('[data-group-row]'); if(row)row.dataset.level=select.value; select.addEventListener('change',()=>{if(row)row.dataset.level=select.value; persistGroupPriorities(); filterGroupRows();});}); document.getElementById('group-matrix-search')?.addEventListener('input',filterGroupRows); document.getElementById('group-level-filter')?.addEventListener('change',filterGroupRows); document.getElementById('group-tag-filter')?.addEventListener('change',filterGroupRows); document.getElementById('group-selection-export')?.addEventListener('click',()=>{const selections=groupPrioritySelects.map(select=>({group:select.dataset.group,level:select.value,suggested:select.dataset.suggested,changed:select.value!==select.dataset.suggested})); const payload={schema_version:1,exported_at:new Date().toISOString(),note:'排除候选不会自动修改微信或配置；交给 Codex 审核后再写入个人 Profile。',selections}; const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json;charset=utf-8'}); const url=URL.createObjectURL(blob); const link=document.createElement('a'); link.href=url; link.download='wechat-group-selection.json'; link.click(); URL.revokeObjectURL(url);}); filterGroupRows();}
    const searchIndex=[]; reportPages.forEach(page=>{const route=page.dataset.route; const pageTitle=pageMeta.find(item=>item.route===route).title; page.querySelectorAll('.markdown-body h2,.markdown-body h3,.markdown-body h4,.markdown-body li,.markdown-body p,.markdown-body summary,.markdown-body blockquote').forEach((node,index)=>{const text=node.textContent.replace(/\s+/g,' ').trim(); if(text.length<4)return; if(!node.id)node.id=`search-${route}-${index}`; const heading=node.matches('h2,h3,h4')?text:(node.closest('details')?.querySelector('summary')?.textContent.trim()||pageTitle); searchIndex.push({route,anchor:node.id,pageTitle,heading,text});});});
    function escapeHtml(value){return value.replace(/[&<>'"]/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'})[character]);}
    function runSearch(){const query=searchInput.value.trim().toLowerCase(); if(!query){searchPanel.hidden=true; searchResults.innerHTML=''; return;} const allMatches=searchIndex.filter(item=>item.text.toLowerCase().includes(query));const matches=allMatches.slice(0,36); searchHead.textContent=`全部分区找到 ${allMatches.length} 条结果${allMatches.length>36?'，显示前 36 条':''}`; searchResults.innerHTML=matches.length?matches.map(item=>{const start=Math.max(0,item.text.toLowerCase().indexOf(query)-45);const snippet=(start?'…':'')+item.text.slice(start,start+150)+(item.text.length>start+150?'…':''); return `<a class="search-result" href="#/${item.route}/${encodeURIComponent(item.anchor)}"><strong>${escapeHtml(item.pageTitle)} · ${escapeHtml(item.heading)}</strong><p>${escapeHtml(snippet)}</p></a>`;}).join(''):'<div class="search-empty">没有找到匹配内容</div>'; searchPanel.hidden=false;}
    window.addEventListener('hashchange',()=>{renderRoute(); searchPanel.hidden=true;}); routeLinks.forEach(link=>link.addEventListener('click',()=>{if(parseRoute().route===link.dataset.routeLink)setTimeout(()=>window.scrollTo(0,0),0);})); groupViewButtons.forEach(button=>button.addEventListener('click',()=>setGroupView(button.dataset.groupViewButton))); searchInput.addEventListener('focus',closeAppearance); searchInput.addEventListener('input',runSearch); searchInput.addEventListener('keydown',event=>{if(event.key==='Escape'){searchInput.value=''; searchPanel.hidden=true; searchInput.blur();}});
    document.addEventListener('keydown',event=>{if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='k'){event.preventDefault(); searchInput.focus();}}); document.addEventListener('click',event=>{if(!event.target.closest('.search-wrap')&&!event.target.closest('.search-panel'))searchPanel.hidden=true;});
    document.getElementById('download-button').addEventListener('click',()=>{const {route}=parseRoute(); const meta=pageMeta.find(page=>page.route===route); const blob=new Blob([markdownByRoute[route]||''],{type:'text/markdown;charset=utf-8'}); const url=URL.createObjectURL(blob); const link=document.createElement('a'); link.href=url; link.download=meta.filename; link.click(); URL.revokeObjectURL(url);});
    document.getElementById('print-button').addEventListener('click',()=>window.print());
    reportPages.forEach(page=>{const outline=document.createElement('nav');outline.className='page-outline';outline.setAttribute('aria-label','本页目录');page.querySelector('.page-content').append(outline);});
    document.querySelectorAll('.key-group-card').forEach(details=>details.addEventListener('toggle',()=>{details.querySelector('.key-group-hint').textContent=details.open?'收起':'点击展开';}));
    document.addEventListener('click',event=>{const link=event.target.closest('a[href^="#/"]');if(link&&link.hash===location.hash){renderRoute();searchPanel.hidden=true;}});
    initGroupSelector(); if(groupViews.length)setGroupView(readSetting('wechat-group-view','topics')); renderRoute();
  </script>
</body>
</html>'''


def render_report_bundle(
    report_dir: Path,
    *,
    output_path: Path | None = None,
    markdown_output_path: Path | None = None,
    title: str | None = None,
) -> tuple[Path, Path, list[ReportSource]]:
    require_sanitizer()
    root = report_dir.expanduser().resolve()
    sources = discover_report_sources(root)
    if not sources:
        raise ValueError(f"报告目录没有 Markdown 文件：{root}")
    pages = build_report_pages(sources)
    if not pages:
        raise ValueError("没有可发布的语义报告，请先完成总览、群聊或联系人编辑。")
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise RuntimeError("生成综合 HTML 需要 pandoc。")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    page_title = title or "微信个人情报库｜综合日报"
    markdown_path = (markdown_output_path or root / "wechat_daily_full.md").expanduser().resolve()
    html_path = (output_path or root / "wechat_daily_report.html").expanduser().resolve()
    markdown_by_route = write_markdown_site(root, pages, portal_path=markdown_path, title=page_title, generated_at=generated_at)
    source_pages = {source.path.resolve(): page for page in pages for source in page.sources}

    fragments: dict[str, str] = {}
    all_markdown: list[str] = []
    all_urls: set[str] = set()
    visible_sources = [source for page in pages for source in page.sources]
    for source in visible_sources:
        text = source.path.read_text(encoding="utf-8", errors="replace")
        all_markdown.append(text)
        all_urls.update(_external_urls(text))
        fragments[source.source_id] = _rewrite_html_links(_pandoc_fragment(source, pandoc), source=source, source_pages=source_pages)

    nav_html = "".join(
        f'<a class="nav-item" href="#/{escape(page.route)}" data-route-link="{escape(page.route)}"><span class="nav-glyph">{escape(page.glyph)}</span><span>{escape(page.nav_label)}</span><span class="nav-count">{_heading_count(page)}</span></a>'
        for page in pages
    )
    mobile_nav_html = "".join(f'<a href="#/{escape(page.route)}" data-route-link="{escape(page.route)}">{escape(page.nav_label)}</a>' for page in pages)
    bottom_routes = [route for route in ("overview", "groups", "contacts", "radar") if any(page.route == route for page in pages)]
    bottom_nav_html = "".join(
        f'<a href="#/{escape(page.route)}" data-route-link="{escape(page.route)}"><span>{escape(page.glyph)}</span>{escape(page.nav_label)}</a>'
        for route in bottom_routes for page in pages if page.route == route
    )
    page_meta = [{"route": page.route, "title": page.title, "description": page.description, "filename": page.filename, "reportTitle": page_title} for page in pages]
    full_text = "\n".join(all_markdown)
    window_match = re.search(r"(?m)^>?\s*时间范围[：:]\s*(.+)$", full_text)
    report_window = f"数据范围：{window_match.group(1).strip()}" if window_match else "数据时间以正文标注为准"
    coverage_path = root / "group-daily" / "group_daily_coverage.json"
    if coverage_path.exists():
        try:
            coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
            if isinstance(coverage, dict) and (coverage.get("failed") or coverage.get("incomplete")):
                report_window += f"｜覆盖缺口：{coverage.get('failed', 0)} 个群读取失败，{coverage.get('incomplete', 0)} 个群未读完"
        except (OSError, ValueError):
            report_window += "｜覆盖记录无法读取"
    html = HTML_TEMPLATE
    group_matrix = _load_group_matrix(root)
    values = {
        "PAGE_TITLE": escape(page_title),
        "REPORT_WINDOW": escape(report_window),
        "NAV_HTML": nav_html,
        "MOBILE_NAV_HTML": mobile_nav_html,
        "BOTTOM_NAV_HTML": bottom_nav_html,
        "BOTTOM_COUNT": str(max(1, len(bottom_routes))),
        "PAGES_HTML": "".join(_render_page(page, fragments, group_matrix) for page in pages),
        "PAGE_META_JSON": json.dumps(page_meta, ensure_ascii=False).replace("</", "<\\/"),
        "MARKDOWN_JSON": json.dumps(markdown_by_route, ensure_ascii=False).replace("</", "<\\/"),
        "GENERATED_AT": escape(generated_at),
        "SOURCE_COUNT": str(len(visible_sources)),
        "LINK_COUNT": str(len(all_urls)),
        "READING_MINUTES": str(_reading_minutes(full_text)),
    }
    html = _replace_tokens(html, values)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(protect_document(html), encoding="utf-8")
    return html_path, markdown_path, sources
