#!/usr/bin/env python3
"""WeChat Slim (微信智能瘦身与人脉透视工具) - CLI 入口主程序.

本模块为命令行交互主入口，所有底层核心能力均已解耦至 engine/ 子模块中：
- engine/common.py: 终端配色、格式化计算、日志与进度条
- engine/scanner.py: 微信账号扫描与存储透视
- engine/cleaner.py: 规则过滤、白名单防删、移动废纸篓与外置归档
- engine/dedup.py: 多群转发特征指纹查重与 APFS 硬链接/废纸篓去重
- engine/web.py: 本地可视化大盘 (WebUI Dashboard)
- engine/whitelist.py: 核心人脉白名单绝对防删
- engine/contact_resolver.py: 联系人/群昵称反解与热重载
- engine/state.py: 历史指标统计与运行时状态持久化
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
import urllib.parse
import webbrowser

# 保证优先从当前目录与 engine 所在目录导入
_CURRENT_DIR = Path(__file__).resolve().parent
if str(_CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(_CURRENT_DIR))
_PROJ_DIR = _CURRENT_DIR / 'projects' / 'wechat-intelligence-hub'
if _PROJ_DIR.exists() and str(_PROJ_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJ_DIR))

# 导入并 Re-export 核心组件，确保向后 100% 兼容已有测试与外部调用
try:
    from engine.common import (
        Colors,
        HAS_RICH,
        _console,
        Table,
        Panel,
        Progress,
        TextColumn,
        BarColumn,
        SpinnerColumn,
        TimeRemainingColumn,
        format_bytes,
        parse_size_str,
        render_progress,
        AuditLogger,
        _audit_logger,
        setup_logger,
    )
    from engine.scanner import (
        AccountProfile,
        ScanCategory,
        discover_accounts,
        scan_directory,
        scan_account,
    )
    from engine.cleaner import (
        SlimResult,
        move_to_trash,
        execute_slimming,
    )
    from engine.dedup import (
        DuplicateGroup,
        compute_fast_hash,
        compute_full_hash,
        find_duplicates,
        execute_dedup,
    )
    from engine.web import (
        WEB_UI_HTML,
        WeChatSlimWebHandler,
        cmd_web,
    )
    from engine.whitelist import (
        WhiteListManager,
        WhiteListRule,
        Contact,
    )
    try:
        from engine.contact_resolver import (
            ContactResolver,
            ContactInfo,
        )
    except ImportError:
        ContactResolver = None
        ContactInfo = None
    from engine.state import (
        StateManager,
        SlimHistoryRecord,
    )
except ImportError:
    from wechat_intelligence_hub.engine.common import (
        Colors, HAS_RICH, _console, Table, Panel, Progress,
        TextColumn, BarColumn, SpinnerColumn, TimeRemainingColumn,
        format_bytes, parse_size_str, render_progress, AuditLogger, _audit_logger, setup_logger,
    )
    from wechat_intelligence_hub.engine.scanner import (
        AccountProfile, ScanCategory, discover_accounts, scan_directory, scan_account,
    )
    from wechat_intelligence_hub.engine.cleaner import (
        SlimResult, move_to_trash, execute_slimming,
    )
    from wechat_intelligence_hub.engine.dedup import (
        DuplicateGroup, compute_fast_hash, compute_full_hash, find_duplicates, execute_dedup,
    )
    from wechat_intelligence_hub.engine.web import (
        WEB_UI_HTML, WeChatSlimWebHandler, cmd_web,
    )
    from wechat_intelligence_hub.engine.whitelist import (
        WhiteListManager, WhiteListRule, Contact,
    )
    try:
        from wechat_intelligence_hub.engine.contact_resolver import (
            ContactResolver, ContactInfo,
        )
    except ImportError:
        ContactResolver = None
        ContactInfo = None
    from wechat_intelligence_hub.engine.state import (
        StateManager, SlimHistoryRecord,
    )

__all__ = [
    'Colors', 'HAS_RICH', '_console', 'Table', 'Panel', 'Progress',
    'format_bytes', 'parse_size_str', 'render_progress', 'AuditLogger', '_audit_logger', 'setup_logger',
    'AccountProfile', 'ScanCategory', 'discover_accounts', 'scan_directory', 'scan_account',
    'SlimResult', 'move_to_trash', 'execute_slimming',
    'DuplicateGroup', 'compute_fast_hash', 'compute_full_hash', 'find_duplicates', 'execute_dedup',
    'WEB_UI_HTML', 'WeChatSlimWebHandler', 'cmd_web',
    'WhiteListManager', 'WhiteListRule', 'Contact',
    'ContactResolver', 'ContactInfo',
    'StateManager', 'SlimHistoryRecord',
    'cmd_scan', 'cmd_clean', 'cmd_dedup', 'cmd_tag', 'cmd_stats', 'interactive_wizard', 'main',
]


def cmd_dedup(args: argparse.Namespace) -> None:
    """执行重复文件查重与硬链接/废纸篓去重."""
    custom_path = getattr(args, 'path', None)
    accounts = discover_accounts(custom_path)
    if not accounts:
        print('[-] 未发现可操作的微信账号目录。')
        return

    acc = accounts[0]
    categories = scan_account(acc)
    types = [t.strip() for t in args.types.split(',') if t.strip()]
    min_size_bytes = parse_size_str(args.min_size)

    action_desc = '转为 APFS 硬链接 (零风险: 微信各群仍能正常打开，但只占 1 份物理磁盘)' if args.action == 'hardlink' else '将多余副本移入系统废纸篓'

    print('=' * 66)
    print('       WeChat Slim - 多群重复文件智能查重与去重 (Phase 2)')
    print('=' * 66)
    print(f'  • 目标账号   : {acc.account_id} ({acc.version_type})')
    print(f'  • 查重范围   : {", ".join(types)}')
    print(f'  • 大小阈值   : 仅检查超过 {args.min_size} 的文件')
    print(f'  • 处理方式   : {action_desc}')
    if args.dry_run:
        print('  • 演练模式   : [Dry-Run - 仅分析展示，不实际修改磁盘]')
    print('-' * 66)
    print('正在计算文件特征哈希指纹，请稍候...')

    groups = find_duplicates(categories, types, min_size_bytes=min_size_bytes)
    actionable_groups = [g for g in groups if g.wasted_count > 0]
    total_wasted_copies = sum(g.wasted_count for g in actionable_groups)
    total_saving_bytes = sum(g.saving_bytes for g in actionable_groups)

    print(f'\n[✓] 查重完成: 发现 {len(actionable_groups)} 组重复文件，包含 {total_wasted_copies:,} 个多余副本')
    print(f'    可释放物理空间: {format_bytes(total_saving_bytes)}')

    if not actionable_groups:
        print('\n[✓] 太棒了！当前范围内未发现占用多份空间的重复文件。')
        return

    # 展示前 5 组最占空间的重复文件
    print('\n[Top 重复文件样本]:')
    for idx, grp in enumerate(actionable_groups[:5], 1):
        print(f"  {idx}. [{format_bytes(grp.file_size)}/个] 共有 {len(grp.files)} 个副本 (可释放 {format_bytes(grp.saving_bytes)}):")
        print(f"     底稿保留: {grp.files[0].name}")
        for dup in grp.files[1:3]:
            print(f"     重复副本: {dup.parent.name}/{dup.name}")
        if len(grp.files) > 3:
            print(f"     ... 等共 {len(grp.files)} 个文件")

    if args.dry_run:
        print('\n[演练完成] 如需实际执行去重，请去掉 --dry-run 参数。')
        return

    if not args.force:
        confirm = input(f'\n确认要对这 {total_wasted_copies:,} 个副本执行 [{args.action}] 去重吗? [y/N]: ').strip().lower()
        if confirm != 'y':
            print('[x] 操作已取消。')
            return

    print('\n正在执行去重处理...')
    done_count, done_bytes = execute_dedup(actionable_groups, action=args.action, dry_run=False)
    state_mgr = StateManager(getattr(args, 'state_path', None))
    state_mgr.record_dedup(done_count, done_bytes, action=args.action)
    _audit_logger.info(
        f"cmd_dedup completed: action={args.action}, processed={done_count}, freed_bytes={done_bytes}"
    )
    print(f'{Colors.GREEN}[✓]{Colors.RESET} 去重成功！已处理 {done_count:,} 个重复副本，成功释放 {Colors.BOLD}{Colors.GREEN}{format_bytes(done_bytes)}{Colors.RESET} 物理磁盘空间！')
    if args.action == 'hardlink':
        print('    提示: 已转换为 APFS 硬链接，微信中所有聊天窗口里的文件依然可原样点击打开！')
    prompt_nps_if_needed(state_mgr)


def cmd_tag(args: argparse.Namespace) -> None:
    """核心人脉与重要会话防删白名单管理."""
    wl_mgr = WhiteListManager(getattr(args, 'whitelist_config', None))

    resolver = None
    if ContactResolver:
        try:
            accs = discover_accounts(getattr(args, 'path', None))
            root_target = accs[0].root_path if accs else getattr(args, 'path', None)
            if root_target:
                resolver = ContactResolver(root_target)
        except Exception:
            pass

    if getattr(args, 'add', None):
        name = args.add
        wxid = getattr(args, 'wxid', None)
        if not wxid:
            print("[-] 添加失败: 请提供 --wxid 参数 (例如: --add \"老婆\" --wxid wxid_xxx)")
            return
        protect = getattr(args, 'protect', 'absolute') or 'absolute'
        keywords = [k.strip() for k in args.keywords.split(',')] if getattr(args, 'keywords', None) else []
        retain_days = getattr(args, 'retain_days', 0) or 0
        rule = wl_mgr.add(name, wxid, protect=protect, keywords=keywords, retain_days=retain_days)
        _audit_logger.info(f"cmd_tag: added rule '{rule.name}' (wxid: {rule.wxid})")
        print(f"{Colors.GREEN}[✓]{Colors.RESET} 成功添加白名单保护规则: {rule.name} (ID: {rule.wxid})")
        print(f"    保护级别: {'绝对保护 (永不删除)' if rule.protect == 'absolute' else f'保留 {rule.retain_days} 天内文件'}")
        if rule.keywords:
            print(f"    包含关键词: {', '.join(rule.keywords)}")
        if resolver:
            cinfo = resolver.resolve(rule.wxid)
            if cinfo and (cinfo.remark or cinfo.nickname):
                print(f"    自动关联微信资料: {cinfo.to_formatted_str()}")
        return

    if getattr(args, 'remove', None):
        ok = wl_mgr.remove(args.remove)
        if ok:
            _audit_logger.info(f"cmd_tag: removed rule '{args.remove}'")
            print(f"{Colors.GREEN}[✓]{Colors.RESET} 成功移除白名单保护规则: {args.remove}")
        else:
            print(f"[-] 未找到匹配的白名单规则: {args.remove}")
        return

    if getattr(args, 'clear', False):
        wl_mgr.clear()
        _audit_logger.info("cmd_tag: cleared all whitelist rules")
        print(f"{Colors.GREEN}[✓]{Colors.RESET} 已清空白名单所有保护规则。")
        return

    # 默认展示所有规则列表
    rules = wl_mgr.list_rules()
    print("=" * 66)
    print(f"{Colors.BOLD}{Colors.MAGENTA}       WeChat Slim - 核心人脉与重要会话防删白名单{Colors.RESET}")
    print("=" * 66)
    if not rules:
        print("  当前暂无白名单规则。")
        print("  提示: 使用以下命令添加核心保护人脉，防止重要文件被误删:")
        print("    python3 wechat_slim.py tag --add \"老婆\" --wxid wxid_xxx --protect absolute")
        print("    python3 wechat_slim.py tag --add \"重要客户\" --wxid xxx@chatroom --keywords \"合同,签约\"")
        print("=" * 66)
        return

    print(f"  当前共生效 {len(rules)} 条白名单保护规则:\n")
    for idx, r in enumerate(rules, 1):
        prot_str = "🔒 绝对保护 (永不删除)" if r.protect == "absolute" else f"⏱️ 保留 {r.retain_days} 天"
        kw_str = f" | 关键词: {', '.join(r.keywords)}" if r.keywords else ""
        cinfo = resolver.resolve(r.wxid) if resolver else None
        extra_str = f" ({cinfo.to_formatted_str()})" if (cinfo and (cinfo.remark or cinfo.nickname)) else ""
        print(f"  {idx}. [{r.name}]{extra_str}")
        print(f"     微信ID/群ID: {r.wxid}")
        print(f"     保护级别   : {prot_str}{kw_str}")
        print(f"     创建时间   : {r.created_at[:19].replace('T', ' ')}")
    print("=" * 66)



def cmd_scan(args: argparse.Namespace) -> None:
    """执行扫描并展示存储透视概览 (包含白名单防删统计)."""
    custom_path = getattr(args, 'path', None)
    accounts = discover_accounts(custom_path)
    if not accounts:
        print('[-] 未在指定或默认微信容器中发现微信数据目录。')
        print('    提示: 请确认微信是否安装，或是否有登录过的账号。')
        return

    wl_mgr = WhiteListManager(getattr(args, 'whitelist_config', None))
    active_rules = wl_mgr.list_rules()

    state_mgr = StateManager(getattr(args, 'state_path', None))
    state_mgr.record_scan()
    _audit_logger.info(f"cmd_scan completed: scanned {len(accounts)} accounts")

    if HAS_RICH and _console:
        _console.rule("[bold green]WeChat Slim - 微信智能存储透视器[/bold green]")
        for idx, acc in enumerate(accounts, 1):
            categories = scan_account(acc)
            total_account_size = sum(c.total_bytes for c in categories.values())
            cleanable_size = sum(c.total_bytes for k, c in categories.items() if not c.is_protected)
            clean_ratio = (cleanable_size / total_account_size * 100) if total_account_size > 0 else 0

            table = Table(title=f"账号 [{acc.account_id}] - {acc.version_type}", show_header=True, header_style="bold magenta")
            table.add_column("存储类别", style="cyan", no_wrap=True)
            table.add_column("文件数量", justify="right", style="dim")
            table.add_column("物理大小", justify="right", style="bold")
            table.add_column("空间占比", justify="right")
            table.add_column("安全状态", justify="center")

            for key, cat in categories.items():
                status_tag = "[bold green]🔒 数据库绝对保护[/bold green]" if cat.is_protected else "[bold yellow]可瘦身[/bold yellow]"
                percent = (cat.total_bytes / total_account_size * 100) if total_account_size > 0 else 0
                table.add_row(
                    cat.name,
                    f"{cat.file_count:,}",
                    format_bytes(cat.total_bytes),
                    f"{percent:5.1f}%",
                    status_tag
                )

            _console.print(table)
            _console.print(Panel.fit(
                f"[bold]总空间占用:[/bold] {format_bytes(total_account_size)}  |  "
                f"[bold yellow]可瘦身潜力:[/bold yellow] {format_bytes(cleanable_size)} ({clean_ratio:.1f}% 可安全瘦身/转存)",
                title="存储健康摘要",
                border_style="green"
            ))

            if active_rules:
                wl_count = 0
                wl_bytes = 0
                for c in categories.values():
                    if c.is_protected:
                        continue
                    for fp, sz, mt in c.files:
                        is_p, _ = wl_mgr.is_protected(fp, mt)
                        if is_p:
                            wl_count += 1
                            wl_bytes += sz
                resolver = ContactResolver(acc.root_path) if ContactResolver else None
                names_list = []
                for r in active_rules[:3]:
                    cinfo = resolver.resolve(r.wxid) if resolver else None
                    if cinfo and (cinfo.remark or cinfo.nickname):
                        names_list.append(f"{r.name} [{cinfo.display_name}]")
                    else:
                        names_list.append(r.name)
                names = ", ".join(names_list)
                if len(active_rules) > 3:
                    names += f" 等 {len(active_rules)} 条"

                _console.print(Panel.fit(
                    f"🛡️ [bold]核心人脉白名单保护[/bold]\n"
                    f"• 活跃规则: {len(active_rules)} 条 ({names})\n"
                    f"• 保护文件: [bold green]{wl_count:,}[/bold green] 个文件 ([bold green]{format_bytes(wl_bytes)}[/bold green] 绝对防删)",
                    border_style="cyan"
                ))
        return

    print('=' * 66)
    print(f'{Colors.BOLD}{Colors.GREEN}       WeChat Slim - 微信智能存储透视器{Colors.RESET}')
    print('=' * 66)

    for idx, acc in enumerate(accounts, 1):
        print(f"\n[账号 {idx}] ID: {acc.account_id} | 版本: {acc.version_type}")
        print(f"路径: {acc.root_path}")
        print("-" * 66)

        categories = scan_account(acc)
        total_account_size = sum(c.total_bytes for c in categories.values())
        cleanable_size = sum(c.total_bytes for k, c in categories.items() if not c.is_protected)

        for key, cat in categories.items():
            status_tag = '[🔒 数据库绝对保护]' if cat.is_protected else '[可瘦身]'
            percent = (cat.total_bytes / total_account_size * 100) if total_account_size > 0 else 0
            size_str = format_bytes(cat.total_bytes).rjust(10)
            count_str = f'({cat.file_count:,} 个文件)'.rjust(16)
            print(f'  • {cat.name.ljust(12)} : {size_str}  {count_str}  {percent:5.1f}%  {status_tag}')

        print('-' * 66)
        clean_ratio = (cleanable_size / total_account_size * 100) if total_account_size > 0 else 0
        print(f'  总空间占用   : {format_bytes(total_account_size)}')
        print(f'  可瘦身潜力   : {format_bytes(cleanable_size)} ({clean_ratio:.1f}% 的空间可被安全瘦身/转存)')

        # 白名单保护统计
        if active_rules:
            wl_count = 0
            wl_bytes = 0
            for c in categories.values():
                if c.is_protected:
                    continue
                for fp, sz, mt in c.files:
                    is_p, _ = wl_mgr.is_protected(fp, mt)
                    if is_p:
                        wl_count += 1
                        wl_bytes += sz
            resolver = ContactResolver(acc.root_path) if ContactResolver else None
            names_list = []
            for r in active_rules[:3]:
                cinfo = resolver.resolve(r.wxid) if resolver else None
                if cinfo and (cinfo.remark or cinfo.nickname):
                    names_list.append(f"{r.name} [{cinfo.display_name}]")
                else:
                    names_list.append(r.name)
            names = ", ".join(names_list)
            if len(active_rules) > 3:
                names += f" 等 {len(active_rules)} 条"
            print('-' * 66)
            print(f"  🛡️ 核心人脉白名单保护:")
            print(f"  • 活跃白名单规则 : {len(active_rules)} 条 ({names})")
            print(f"  • 已锁定保护文件 : {wl_count:,} 个文件 ({format_bytes(wl_bytes)} 空间受白名单绝对保护，绝不误删)")
    print()



def cmd_clean(args: argparse.Namespace) -> None:
    """执行瘦身清理或外置归档."""
    custom_path = getattr(args, 'path', None)
    accounts = discover_accounts(custom_path)
    if not accounts:
        print('[-] 未发现可操作的微信账号目录。')
        return

    acc = accounts[0]
    categories = scan_account(acc)
    types = [t.strip() for t in args.types.split(',') if t.strip()]
    min_size_bytes = parse_size_str(args.min_size)
    archive_dir = Path(args.archive_to) if args.archive_to else None
    wl_mgr = WhiteListManager(getattr(args, 'whitelist_config', None))

    action_name = f'无损转存归档至 [{archive_dir}]' if archive_dir else '安全移入系统废纸篓 (Trash)'

    print('=' * 66)
    print('  WeChat Slim - 执行配置')
    print('=' * 66)
    print(f'  • 目标账号   : {acc.account_id} ({acc.version_type})')
    print(f'  • 清理类型   : {", ".join(types)}')
    print(f'  • 时间过滤   : 清理 {args.days} 天前的文件' if args.days > 0 else '  • 时间过滤   : 不限时间')
    print(f'  • 大小过滤   : 仅处理超过 {args.min_size} 的文件' if min_size_bytes > 0 else '  • 大小过滤   : 不限文件大小')
    print(f'  • 执行动作   : {action_name}')
    if args.dry_run:
        print('  • 模拟运行   : [演练模式 Dry-Run - 不实际移动或删除任何文件]')
    print('-' * 66)

    pre_res = execute_slimming(
        acc, categories, args.days, min_size_bytes, types, dry_run=True, archive_to=archive_dir, whitelist_mgr=wl_mgr
    )

    print(f'  预估影响     : 共计 {pre_res.freed_count:,} 个文件，可释放 {format_bytes(pre_res.freed_bytes)} 空间')
    if pre_res.protected_count > 0:
        print(f'  🛡️ 白名单保护: 已自动跳过并锁定保护 {pre_res.protected_count:,} 个核心联系人文件 ({format_bytes(pre_res.protected_bytes)} 空间)')

    if pre_res.freed_count == 0:
        print('\n[✓] 没有符合当前过滤条件的文件，无需清理。')
        return

    if not args.force and not args.dry_run:
        confirm = input(f'\n确认要对这 {pre_res.freed_count:,} 个文件执行 {action_name} 吗? [y/N]: ').strip().lower()
        if confirm != 'y':
            print('[x] 操作已取消。')
            return

    if not args.dry_run:
        print('\n正在处理中，请稍候...')
        act_res = execute_slimming(
            acc, categories, args.days, min_size_bytes, types, dry_run=False, archive_to=archive_dir, whitelist_mgr=wl_mgr
        )
        state_mgr = StateManager(getattr(args, 'state_path', None))
        state_mgr.record_clean(
            freed_count=act_res.freed_count,
            freed_bytes=act_res.freed_bytes,
            protected_count=act_res.protected_count,
            protected_bytes=act_res.protected_bytes,
            is_archive=bool(archive_dir),
        )
        _audit_logger.info(
            f"cmd_clean completed: freed_count={act_res.freed_count}, freed_bytes={act_res.freed_bytes}, "
            f"protected_count={act_res.protected_count}, protected_bytes={act_res.protected_bytes}, "
            f"is_archive={bool(archive_dir)}"
        )
        print(f'{Colors.GREEN}[✓]{Colors.RESET} 处理完成！成功释放 {Colors.BOLD}{Colors.GREEN}{format_bytes(act_res.freed_bytes)}{Colors.RESET} 空间（处理了 {act_res.freed_count:,} 个文件）。')
        if act_res.protected_count > 0:
            print(f'    🛡️ 白名单防删: 严格保护了 {act_res.protected_count:,} 个核心联系人文件未被触碰。')
        if not archive_dir:
            print('    提示: 文件已被安全放入废纸篓。如需彻底释放磁盘空间，请清空废纸篓。')
        else:
            print(f'    提示: 所有文件已完整保存至外置目录: {archive_dir}')
        prompt_nps_if_needed(state_mgr)
    else:
        print('\n[演练完成] 实际执行时请去掉 --dry-run 参数。')


def prompt_nps_if_needed(state_mgr: StateManager) -> None:
    """非阻塞展示里程碑提示，避免因等待交互式输入阻塞脚本或 Agent 流水线."""
    if not state_mgr.should_trigger_nps():
        return
    state_mgr.mark_nps_prompted()
    print(f"\n{Colors.YELLOW}🌟 感谢支持：您已累计使用 WeChat Slim 安全释放了 {Colors.GREEN}{format_bytes(state_mgr.total_freed_bytes)}{Colors.YELLOW} 空间！{Colors.RESET}")
    print(f"   欢迎在 GitHub 提交反馈与 Star 支持: https://github.com/LuckTerence/CleanYourWechatTool\n")


def cmd_stats(args: argparse.Namespace) -> None:
    """查看历史累计瘦身统计与操作记录."""
    state_path = getattr(args, 'state_path', None)
    state_mgr = StateManager(state_path)

    log_dir = Path.home() / ".wechat_slim"
    audit_log = log_dir / "audit.log"
    nps_str = f"{state_mgr.nps_score} / 10 分" if state_mgr.nps_score is not None else "尚未打分 (使用 10 次后自动开启反馈)"

    if HAS_RICH and _console:
        _console.rule("[bold blue]WeChat Slim - 历史累计瘦身统计与审计大盘[/bold blue]")
        stats_summary = (
            f"[bold]状态存储路径:[/bold] {state_mgr.state_path}\n"
            f"[bold]审计日志路径:[/bold] {audit_log}\n"
            f"• 累计运行: [bold green]{state_mgr.total_runs}[/bold green] 次  "
            f"(扫描: {state_mgr.total_scans} | 清理: {state_mgr.total_cleans} | 去重: {state_mgr.total_dedups})\n"
            f"• 累计释放空间: [bold green]{format_bytes(state_mgr.total_freed_bytes)}[/bold green]\n"
            f"• 累计保护文件: [bold cyan]{format_bytes(state_mgr.total_protected_bytes)}[/bold cyan] (白名单核心防删)\n"
            f"• NPS 满意度  : [bold yellow]{nps_str}[/bold yellow]"
        )
        _console.print(Panel(stats_summary, title="总览看板", border_style="blue"))

        if state_mgr.history:
            h_table = Table(title="最近操作记录", show_header=True, header_style="bold cyan")
            h_table.add_column("序号", justify="center", style="dim")
            h_table.add_column("操作时间", style="cyan")
            h_table.add_column("操作类型", style="bold")
            h_table.add_column("影响文件", justify="right")
            h_table.add_column("释放空间", justify="right", style="green")
            h_table.add_column("白名单保护", justify="right", style="cyan")

            action_map = {
                "clean": "清理瘦身",
                "archive": "外置归档",
                "dedup_hardlink": "APFS硬链接去重",
                "dedup_trash": "废纸篓去重",
                "scan": "存储扫描",
            }
            recent = state_mgr.history[-5:]
            for idx, rec in enumerate(reversed(recent), 1):
                ts = rec.timestamp[:19].replace("T", " ")
                act_name = action_map.get(rec.action, rec.action)
                h_table.add_row(
                    str(idx),
                    ts,
                    act_name,
                    f"{rec.count:,} 个",
                    format_bytes(rec.freed_bytes),
                    format_bytes(rec.protected_bytes) if rec.protected_bytes > 0 else "-"
                )
            _console.print(h_table)
        return

    print("=" * 66)
    print(f"{Colors.BOLD}{Colors.BLUE}       WeChat Slim - 历史累计瘦身统计与审计大盘{Colors.RESET}")
    print("=" * 66)
    print(f"  • 状态存储路径 : {state_mgr.state_path}")
    print(f"  • 审计日志路径 : {audit_log}")
    print("-" * 66)
    print(f"  • 累计运行次数 : {Colors.BOLD}{state_mgr.total_runs}{Colors.RESET} 次")
    print(f"  • 累计空间扫描 : {state_mgr.total_scans} 次")
    print(f"  • 累计瘦身清理 : {state_mgr.total_cleans} 次")
    print(f"  • 累计查重去重 : {state_mgr.total_dedups} 次")
    print(f"  • 累计释放空间 : {Colors.BOLD}{Colors.GREEN}{format_bytes(state_mgr.total_freed_bytes)}{Colors.RESET}")
    print(f"  • 累计保护文件 : {Colors.CYAN}{format_bytes(state_mgr.total_protected_bytes)}{Colors.RESET} (白名单核心防删)")
    nps_str = f"{state_mgr.nps_score} / 10 分" if state_mgr.nps_score is not None else "尚未打分 (使用 10 次后自动开启反馈)"
    print(f"  • NPS 满意度   : {Colors.YELLOW}{nps_str}{Colors.RESET}")
    print("-" * 66)

    if not state_mgr.history:
        print("  当前尚无详细历史操作记录。")
    else:
        print("  [最近 5 次操作记录]:")
        recent = state_mgr.history[-5:]
        for idx, rec in enumerate(reversed(recent), 1):
            ts = rec.timestamp[:19].replace("T", " ")
            action_map = {
                "clean": "清理瘦身",
                "archive": "外置归档",
                "dedup_hardlink": "APFS硬链接去重",
                "dedup_trash": "废纸篓去重",
                "scan": "存储扫描",
            }
            act_name = action_map.get(rec.action, rec.action)
            print(f"  {idx}. [{ts}] {act_name}")
            print(f"     影响文件: {rec.count:,} 个 | 释放空间: {format_bytes(rec.freed_bytes)}")
            if rec.protected_bytes > 0:
                print(f"     白名单保护: {format_bytes(rec.protected_bytes)}")
            if rec.note:
                print(f"     备注: {rec.note}")
    print("=" * 66)


def interactive_wizard() -> None:
    """极简交互式向导."""
    accounts = discover_accounts()
    if not accounts:
        print('[-] 未发现微信数据目录。')
        return

    acc = accounts[0]
    categories = scan_account(acc)
    total_size = sum(c.total_bytes for c in categories.values())
    cleanable_size = sum(c.total_bytes for k, c in categories.items() if not c.is_protected)

    print('=' * 66)
    print(f'{Colors.BOLD}{Colors.GREEN}       WeChat Slim - 微信智能瘦身与无损归档工具 (Mac版){Colors.RESET}')
    print('=' * 66)
    print(f'{Colors.GREEN}[✓]{Colors.RESET} 自动定位账号: {Colors.BOLD}{acc.account_id}{Colors.RESET} ({acc.version_type})')
    print(f'    总占用: {format_bytes(total_size)} | 瘦身潜力: {Colors.BOLD}{Colors.GREEN}{format_bytes(cleanable_size)}{Colors.RESET}')
    print('-' * 66)

    print('\n请选择要执行的操作:')
    print('  [1] 快速瘦身 (推荐: 清理 90 天前且 >10MB 的视频/文件，移入废纸篓)')
    print('  [2] 极限瘦身 (清理所有 30 天前的缓存、视频与下载文件)')
    print('  [3] 仅清理临时缓存 (仅清理 cache/temp，绝不触碰任何聊天文件)')
    print('  [4] 存储空间详细扫描 (查看各分类占用与白名单保护统计)')
    print('  [5] 智能查重去重 (多群重复转发秒级查重，转换为 APFS 硬链接释放空间)')
    print('  [6] 启动网页大盘 (启动本地现代化 WebUI 并在浏览器中查看)')
    print('  [7] 核心人脉白名单管理 (查看或添加家人、老板、重要客户防删名单)')
    print('  [8] 历史使用统计与审计 (查看累计释放空间与操作记录)')
    print('  [q] 退出')

    choice = input('\n请输入选项 [1-8/q]: ').strip().lower()
    if choice == '1':
        args = argparse.Namespace(
            types='video,file',
            days=90,
            min_size='10MB',
            dry_run=False,
            archive_to=None,
            force=False,
        )
        cmd_clean(args)
    elif choice == '2':
        args = argparse.Namespace(
            types='video,file,cache',
            days=30,
            min_size='0B',
            dry_run=False,
            archive_to=None,
            force=False,
        )
        cmd_clean(args)
    elif choice == '3':
        args = argparse.Namespace(
            types='cache',
            days=0,
            min_size='0B',
            dry_run=False,
            archive_to=None,
            force=False,
        )
        cmd_clean(args)
    elif choice == '4':
        cmd_scan(argparse.Namespace())
    elif choice == '5':
        args = argparse.Namespace(
            types='video,file,attach',
            min_size='500KB',
            action='hardlink',
            dry_run=False,
            force=False,
            path=None,
        )
        cmd_dedup(args)
    elif choice == '6':
        cmd_web(argparse.Namespace(port=8080, path=None, no_browser=False))
    elif choice == '7':
        cmd_tag(argparse.Namespace(add=None, remove=None, clear=False, list=True))
    elif choice == '8':
        cmd_stats(argparse.Namespace(state_path=None))
    else:
        print('已退出。')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='WeChat Slim - 微信智能存储透视与安全瘦身工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest='subcommand')

    scan_p = subparsers.add_parser('scan', help='扫描并展示微信存储空间深度分布')
    scan_p.add_argument('--path', default=None, help='指定自定义微信存储目录 (默认: 自动发现系统微信目录)')
    scan_p.add_argument('--whitelist-config', default=None, help=argparse.SUPPRESS)
    scan_p.add_argument('--state-path', default=None, help=argparse.SUPPRESS)

    clean_p = subparsers.add_parser('clean', help='执行文件瘦身或外置归档')
    clean_p.add_argument('--path', default=None, help='指定自定义微信存储目录 (默认: 自动发现系统微信目录)')
    clean_p.add_argument('--days', type=int, default=90, help='清理多少天前的文件 (默认: 90 天，0 为不限时间)')
    clean_p.add_argument('--min-size', default='0B', help='文件最小大小阈值 (例如: 20MB, 10MB，默认: 0B)')
    clean_p.add_argument('--types', default='video,file,cache', help='清理文件类型，逗号分隔 (可选: video,file,attach,cache)')
    clean_p.add_argument('--dry-run', action='store_true', help='模拟预演，只统计不实际移动任何文件')
    clean_p.add_argument('--archive-to', default=None, help='指定外置移动硬盘或备份目录 (将文件安全移动至该目录，而非废纸篓)')
    clean_p.add_argument('-f', '--force', action='store_true', help='跳过确认提示直接执行')
    clean_p.add_argument('--whitelist-config', default=None, help=argparse.SUPPRESS)
    clean_p.add_argument('--state-path', default=None, help=argparse.SUPPRESS)

    dedup_p = subparsers.add_parser('dedup', help='多群转发重复文件智能查重与去重 (Phase 2)')
    dedup_p.add_argument('--path', default=None, help='指定自定义微信存储目录 (默认: 自动发现系统微信目录)')
    dedup_p.add_argument('--types', default='video,file,attach', help='查重类型，逗号分隔 (可选: video,file,attach)')
    dedup_p.add_argument('--min-size', default='500KB', help='查重最小文件大小 (例如: 1MB, 500KB，默认: 500KB)')
    dedup_p.add_argument('--action', choices=['hardlink', 'trash'], default='hardlink', help='去重动作: hardlink (转为硬链接，零风险) 或 trash (移入废纸篓)')
    dedup_p.add_argument('--dry-run', action='store_true', help='模拟预演，只分析展示不实际修改')
    dedup_p.add_argument('-f', '--force', action='store_true', help='跳过确认提示直接执行')
    dedup_p.add_argument('--state-path', default=None, help=argparse.SUPPRESS)

    web_p = subparsers.add_parser('web', help='启动本地可视化大盘 (WebUI Dashboard)')
    web_p.add_argument('--port', type=int, default=8080, help='指定本地网页端口 (默认: 8080)')
    web_p.add_argument('--path', default=None, help='指定自定义微信存储目录 (默认: 自动发现系统微信目录)')
    web_p.add_argument('--no-browser', action='store_true', help='不自动打开默认浏览器')
    web_p.add_argument('--whitelist-config', default=None, help=argparse.SUPPRESS)
    web_p.add_argument('--state-path', default=None, help=argparse.SUPPRESS)

    tag_p = subparsers.add_parser('tag', help='核心人脉与重要会话防删白名单管理')
    tag_p.add_argument('--add', default=None, metavar='NAME', help='受保护人脉/群名称 (如: "老婆", "重要客户")')
    tag_p.add_argument('--wxid', default=None, help='联系人微信号/wxid/群ID (如: "wxid_xxx", "xxx@chatroom")')
    tag_p.add_argument('--protect', choices=['absolute', 'retain_days'], default='absolute', help='保护级别: absolute (绝对保护永不删) 或 retain_days (保留N天内文件)')
    tag_p.add_argument('--keywords', default=None, help='保护文件名关键词，逗号分隔 (如: "合同,宝宝,结婚")')
    tag_p.add_argument('--retain-days', type=int, default=0, help='保留天数 (配合 --protect retain_days 使用)')
    tag_p.add_argument('--remove', default=None, metavar='NAME_OR_WXID', help='移除指定的白名单规则')
    tag_p.add_argument('--list', action='store_true', help='列出所有当前生效的白名单规则')
    tag_p.add_argument('--clear', action='store_true', help='清空所有白名单规则')
    tag_p.add_argument('--whitelist-config', default=None, help=argparse.SUPPRESS)

    stats_p = subparsers.add_parser('stats', help='查看历史累计瘦身统计与操作记录')
    stats_p.add_argument('--state-path', default=None, help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.subcommand == 'scan':
        cmd_scan(args)
    elif args.subcommand == 'clean':
        cmd_clean(args)
    elif args.subcommand == 'dedup':
        cmd_dedup(args)
    elif args.subcommand == 'web':
        cmd_web(args)
    elif args.subcommand == 'tag':
        cmd_tag(args)
    elif args.subcommand == 'stats':
        cmd_stats(args)
    else:
        interactive_wizard()


if __name__ == '__main__':
    main()
