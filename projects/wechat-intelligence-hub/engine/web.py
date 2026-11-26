"""WeChat Slim local WebUI dashboard and HTTP request handler."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
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

try:
    from engine.common import Colors, format_bytes, parse_size_str, _audit_logger
    from engine.scanner import discover_accounts, scan_account
    from engine.cleaner import execute_slimming
    from engine.dedup import find_duplicates, execute_dedup
    from engine.whitelist import WhiteListManager, WhiteListRule
    from engine.state import StateManager
    try:
        from engine.contact_resolver import ContactResolver
    except ImportError:
        ContactResolver = None
except ImportError:
    from .common import Colors, format_bytes, parse_size_str, _audit_logger
    from .scanner import discover_accounts, scan_account
    from .cleaner import execute_slimming
    from .dedup import find_duplicates, execute_dedup
    from .whitelist import WhiteListManager, WhiteListRule
    from .state import StateManager
    try:
        from .contact_resolver import ContactResolver
    except ImportError:
        ContactResolver = None
WEB_UI_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WeChat Slim - 微信智能存储透视与安全瘦身大盘</title>
    <style>
        :root {
            --bg: #f5f6f8;
            --card-bg: #ffffff;
            --text-main: #1d1d1f;
            --text-sub: #86868b;
            --border: #e5e5ea;
            --primary: #0071e3;
            --primary-hover: #0077ed;
            --success: #34c759;
            --warning: #ff9500;
            --danger: #ff3b30;
            --radius: 12px;
            --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: var(--font); background: var(--bg); color: var(--text-main); line-height: 1.5; padding: 24px 16px; }
        .container { max-width: 980px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
        .logo { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
        .badge { background: #e8f2ff; color: var(--primary); padding: 4px 10px; border-radius: 20px; font-size: 12px; font-weight: 600; }
        .grid-stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: var(--card-bg); border-radius: var(--radius); border: 1px solid var(--border); padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.03); }
        .stat-label { font-size: 13px; color: var(--text-sub); font-weight: 500; margin-bottom: 6px; }
        .stat-val { font-size: 26px; font-weight: 700; letter-spacing: -0.5px; }
        .stat-desc { font-size: 12px; color: var(--text-sub); margin-top: 4px; }
        .progress-bar-container { background: #e5e5ea; border-radius: 8px; height: 14px; overflow: hidden; display: flex; margin: 16px 0 8px 0; }
        .progress-seg { height: 100%; transition: width 0.3s; }
        .bg-attach { background: #0071e3; }
        .bg-video { background: #5856d6; }
        .bg-file { background: #34c759; }
        .bg-cache { background: #ff9500; }
        .bg-db { background: #8e8e93; }
        .legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--text-sub); margin-bottom: 24px; }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
        .tabs { display: flex; gap: 8px; border-bottom: 1px solid var(--border); margin-bottom: 20px; }
        .tab-btn { background: none; border: none; padding: 10px 16px; font-size: 14px; font-weight: 600; color: var(--text-sub); cursor: pointer; border-bottom: 2px solid transparent; }
        .tab-btn.active { color: var(--primary); border-bottom-color: var(--primary); }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .form-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 16px; }
        .form-group label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
        .form-group input, .form-group select { width: 100%; padding: 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 14px; }
        .checkbox-group { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; margin: 12px 0; font-size: 13px; }
        .btn-group { display: flex; gap: 12px; margin-top: 18px; }
        .btn { padding: 10px 20px; border-radius: 8px; border: none; font-size: 14px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
        .btn-primary { background: var(--primary); color: white; }
        .btn-primary:hover { background: var(--primary-hover); }
        .btn-secondary { background: #e5e5ea; color: var(--text-main); }
        .btn-secondary:hover { background: #d1d1d6; }
        .btn-success { background: var(--success); color: white; }
        .console { background: #1c1c1e; color: #30d158; padding: 16px; border-radius: 8px; font-family: ui-monospace, Menlo, monospace; font-size: 12px; max-height: 200px; overflow-y: auto; white-space: pre-wrap; margin-top: 20px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); }
        th { color: var(--text-sub); font-weight: 600; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <div class="logo">🧹 WeChat Slim <span class="badge" id="accountBadge">正在连接...</span></div>
        <div style="font-size: 13px; color: var(--text-sub);" id="accountPath"></div>
    </header>

    <div class="grid-stats">
        <div class="card">
            <div class="stat-label">微信总占用空间</div>
            <div class="stat-val" id="totalSize">--</div>
            <div class="stat-desc" id="totalFiles">正在扫描数据...</div>
        </div>
        <div class="card">
            <div class="stat-label">可安全释放潜力</div>
            <div class="stat-val" style="color: var(--success);" id="cleanableSize">--</div>
            <div class="stat-desc" id="cleanableRatio">大文件与缓存可瘦身</div>
        </div>
        <div class="card">
            <div class="stat-label">核心数据库与文字消息</div>
            <div class="stat-val" style="color: var(--text-sub);" id="dbSize">--</div>
            <div class="stat-desc">🔒 100% 绝对保护，绝不误删</div>
        </div>
    </div>

    <div class="card" style="margin-bottom: 24px;">
        <div style="font-size: 14px; font-weight: 600; margin-bottom: 8px;">存储空间结构分布</div>
        <div class="progress-bar-container" id="progressBar"></div>
        <div class="legend" id="legend"></div>
    </div>

    <div class="card">
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('slim')">🚀 智能安全瘦身</button>
            <button class="tab-btn" onclick="switchTab('dedup')">🔗 多群查重 (APFS硬链接)</button>
            <button class="tab-btn" onclick="switchTab('whitelist')">🛡️ 核心人脉防删白名单</button>
            <button class="tab-btn" onclick="switchTab('history')">📊 历史累计与审计</button>
            <button class="tab-btn" onclick="switchTab('details')">📋 存储明细</button>
        </div>

        <!-- 瘦身 Tab -->
        <div id="tab-slim" class="tab-content active">
            <div class="form-row">
                <div class="form-group">
                    <label>时间范围</label>
                    <select id="slimDays">
                        <option value="90" selected>清理 90 天前的文件 (推荐)</option>
                        <option value="30">清理 30 天前的文件 (深度)</option>
                        <option value="180">清理 180 天前的文件 (保守)</option>
                        <option value="0">不限时间 (全量清理)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>单文件大小阈值</label>
                    <select id="slimMinSize">
                        <option value="10MB" selected>大于 10MB 的大文件 (推荐)</option>
                        <option value="20MB">大于 20MB 的超大文件</option>
                        <option value="50MB">大于 50MB 的特大视频/文档</option>
                        <option value="0B">不限大小 (清理所有选定类型)</option>
                    </select>
                </div>
            </div>
            <div class="form-group">
                <label>清理类型</label>
                <div class="checkbox-group">
                    <label><input type="checkbox" id="typeVideo" checked> 聊天视频 (msg/video)</label>
                    <label><input type="checkbox" id="typeFile" checked> 接收的文档 (msg/file)</label>
                    <label><input type="checkbox" id="typeAttach"> 聊天多媒体图片 (msg/attach)</label>
                    <label><input type="checkbox" id="typeCache" checked> 临时运行缓存 (cache/temp)</label>
                </div>
            </div>
            <div class="form-group" style="margin-top: 12px;">
                <label>外置硬盘归档目录 (可选，留空则默认移入废纸篓)</label>
                <input type="text" id="archivePath" placeholder="例如: /Volumes/MySSD/WeChatBackup (自动建立对应文件夹保持结构)">
            </div>
            <div class="btn-group">
                <button class="btn btn-secondary" onclick="executeSlim(true)">🔍 模拟演练 (Dry-Run)</button>
                <button class="btn btn-primary" onclick="executeSlim(false)">⚡ 开始执行安全瘦身</button>
            </div>
        </div>

        <!-- 查重 Tab -->
        <div id="tab-dedup" class="tab-content">
            <p style="font-size: 13px; color: var(--text-sub); margin-bottom: 16px;">
                在多群中被多次转发的相同大文件，将通过 APFS 硬链接秒级去重：所有聊天窗口里依然可正常打开文件，但在物理 SSD 磁盘上只占 1 份空间！
            </p>
            <div class="form-row">
                <div class="form-group">
                    <label>查重文件大小门槛</label>
                    <select id="dedupMinSize">
                        <option value="500KB" selected>大于 500KB (推荐)</option>
                        <option value="1MB">大于 1MB</option>
                        <option value="5MB">大于 5MB (仅查大视频/文档)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>去重动作</label>
                    <select id="dedupAction">
                        <option value="hardlink" selected>替换为 APFS 硬链接 (零风险，强烈推荐)</option>
                        <option value="trash">移入 macOS 废纸篓</option>
                    </select>
                </div>
            </div>
            <div class="btn-group">
                <button class="btn btn-secondary" onclick="scanDedup()">🔍 扫描重复文件</button>
                <button class="btn btn-success" onclick="executeDedup()">🔗 执行硬链接秒级去重</button>
            </div>
            <div id="dedupResults" style="margin-top: 16px;"></div>
        </div>

        <!-- 白名单 Tab -->
        <div id="tab-whitelist" class="tab-content">
            <p style="font-size: 13px; color: var(--text-sub); margin-bottom: 16px;">
                加入白名单的核心人脉（家人、老板、重要客户）与其聊天中的文件、视频在任何清理动作中都将受到<b>绝对隔离保护</b>，系统会自动识别并跳过，绝不误删。
            </p>
            <div class="form-row">
                <div class="form-group">
                    <label>人脉/群备注名</label>
                    <input type="text" id="wlName" placeholder="例如: 老婆、公司财务群、核心客户A">
                </div>
                <div class="form-group">
                    <label>微信ID / 群ID (wxid)</label>
                    <input type="text" id="wlWxid" placeholder="例如: wxid_xxx 或 xxx@chatroom">
                </div>
                <div class="form-group">
                    <label>保护级别</label>
                    <select id="wlProtect">
                        <option value="absolute" selected>绝对保护 (永不删除)</option>
                        <option value="retain_days">保留指定天数内文件</option>
                    </select>
                </div>
            </div>
            <div class="form-row">
                <div class="form-group">
                    <label>保护文件名关键词 (可选，逗号分隔)</label>
                    <input type="text" id="wlKeywords" placeholder="例如: 合同,发票,签约,宝宝照片">
                </div>
                <div class="form-group">
                    <label>保留天数 (配合保留天数选项)</label>
                    <input type="number" id="wlRetainDays" value="365" placeholder="默认: 365 天">
                </div>
            </div>
            <div class="btn-group">
                <button class="btn btn-primary" onclick="addWhitelistRule()">➕ 添加防删白名单保护</button>
                <button class="btn btn-secondary" onclick="loadWhitelist()">🔄 刷新列表</button>
            </div>

            <div style="margin-top: 20px;">
                <div style="font-size: 14px; font-weight: 600; margin-bottom: 8px;">已生效的防删白名单规则</div>
                <table>
                    <thead>
                        <tr><th>保护对象</th><th>微信ID / 群ID</th><th>保护级别</th><th>指定关键词</th><th>创建时间</th><th>操作</th></tr>
                    </thead>
                    <tbody id="whitelistBody"></tbody>
                </table>
            </div>
        </div>

        <!-- 历史与审计 Tab -->
        <div id="tab-history" class="tab-content">
            <p style="font-size: 13px; color: var(--text-sub); margin-bottom: 16px;">
                系统全生命周期运行指标与本地审计跟踪。每次清理、查重与外置归档均受严密记录。
            </p>
            <div class="grid-stats" style="margin-bottom: 16px;">
                <div class="card" style="padding: 14px;">
                    <div class="stat-label">累计运行次数</div>
                    <div class="stat-val" id="histRuns">--</div>
                    <div class="stat-desc" id="histScansCleans">--</div>
                </div>
                <div class="card" style="padding: 14px;">
                    <div class="stat-label">累计释放空间</div>
                    <div class="stat-val" style="color: var(--success);" id="histFreed">--</div>
                    <div class="stat-desc">SSD 磁盘真实释放</div>
                </div>
                <div class="card" style="padding: 14px;">
                    <div class="stat-label">白名单锁定保护</div>
                    <div class="stat-val" style="color: var(--primary);" id="histProtected">--</div>
                    <div class="stat-desc">严格守护跳过的文件空间</div>
                </div>
                <div class="card" style="padding: 14px;">
                    <div class="stat-label">NPS 推荐度评分</div>
                    <div class="stat-val" style="color: var(--warning);" id="histNps">--</div>
                    <div class="stat-desc">用户满意度</div>
                </div>
            </div>

            <div style="font-size: 14px; font-weight: 600; margin-bottom: 8px;">最近操作历史明细</div>
            <table>
                <thead>
                    <tr><th>时间</th><th>操作类型</th><th>影响文件数</th><th>释放空间</th><th>保护空间</th><th>备注</th></tr>
                </thead>
                <tbody id="historyBody"></tbody>
            </table>

            <div style="margin-top: 20px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                    <div style="font-size: 14px; font-weight: 600;">本地安全审计日志 (最近 30 条)</div>
                    <div style="font-size: 12px; color: var(--text-sub);" id="auditLogPath"></div>
                </div>
                <div class="console" id="auditLogConsole" style="max-height: 180px;">正在加载审计日志...</div>
            </div>
        </div>

        <!-- 明细 Tab -->
        <div id="tab-details" class="tab-content">
            <table>
                <thead>
                    <tr><th>目录类别</th><th>占用大小</th><th>文件数量</th><th>占比</th><th>安全状态</th></tr>
                </thead>
                <tbody id="detailsBody"></tbody>
            </table>
        </div>

        <div class="console" id="logConsole">> WeChat Slim 就绪。等待指令...</div>
    </div>
</div>

<script>
    let globalData = null;
    function log(msg) {
        const c = document.getElementById('logConsole');
        c.innerText += '\\n' + msg;
        c.scrollTop = c.scrollHeight;
    }

    function switchTab(name) {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        event.target.classList.add('active');
        document.getElementById('tab-' + name).classList.add('active');
        if (name === 'whitelist') loadWhitelist();
        if (name === 'history') loadHistory();
    }

    async function loadStats() {
        log('正在扫描本地微信存储...');
        const res = await fetch('/api/stats');
        globalData = await res.json();
        renderStats();
        loadWhitelist();
        loadHistory();
    }

    function renderStats() {
        if (!globalData || !globalData.account) return;
        document.getElementById('accountBadge').innerText = globalData.account.id + ' (' + globalData.account.version + ')';
        document.getElementById('accountPath').innerText = globalData.account.root_path;
        document.getElementById('totalSize').innerText = globalData.total_size_str;
        document.getElementById('totalFiles').innerText = globalData.total_files + ' 个文件';
        document.getElementById('cleanableSize').innerText = globalData.cleanable_size_str;
        document.getElementById('cleanableRatio').innerText = globalData.cleanable_ratio + '% 空间可被安全瘦身';
        document.getElementById('dbSize').innerText = globalData.categories.db.size_str;

        // Progress Bar
        const bar = document.getElementById('progressBar');
        const legend = document.getElementById('legend');
        bar.innerHTML = '';
        legend.innerHTML = '';

        const colors = { attach: 'bg-attach', video: 'bg-video', file: 'bg-file', cache: 'bg-cache', db: 'bg-db' };
        const hex = { attach: '#0071e3', video: '#5856d6', file: '#34c759', cache: '#ff9500', db: '#8e8e93' };

        for (let k in globalData.categories) {
            const cat = globalData.categories[k];
            const seg = document.createElement('div');
            seg.className = 'progress-seg ' + (colors[k] || 'bg-db');
            seg.style.width = cat.percent + '%';
            seg.title = cat.name + ': ' + cat.size_str;
            bar.appendChild(seg);

            legend.innerHTML += `<div class="legend-item"><span class="legend-dot" style="background:${hex[k] || '#8e8e93'}"></span>${cat.name} (${cat.size_str}, ${cat.percent}%)</div>`;
        }

        // Details Table
        const tbody = document.getElementById('detailsBody');
        tbody.innerHTML = '';
        for (let k in globalData.categories) {
            const cat = globalData.categories[k];
            const badge = cat.is_protected ? '<span style="color:#8e8e93; font-weight:600;">🔒 绝对保护</span>' : '<span style="color:#34c759; font-weight:600;">✓ 可瘦身</span>';
            tbody.innerHTML += `<tr><td><b>${cat.name}</b><br><small style="color:#86868b">${cat.description}</small></td><td>${cat.size_str}</td><td>${cat.file_count}</td><td>${cat.percent}%</td><td>${badge}</td></tr>`;
        }
        log('扫描完成: 微信总占用 ' + globalData.total_size_str + '，可瘦身潜力 ' + globalData.cleanable_size_str);
    }

    async function executeSlim(isDryRun) {
        const types = [];
        if (document.getElementById('typeVideo').checked) types.push('video');
        if (document.getElementById('typeFile').checked) types.push('file');
        if (document.getElementById('typeAttach').checked) types.push('attach');
        if (document.getElementById('typeCache').checked) types.push('cache');

        const payload = {
            days: parseInt(document.getElementById('slimDays').value),
            min_size: document.getElementById('slimMinSize').value,
            types: types.join(','),
            archive_to: document.getElementById('archivePath').value.trim() || null,
            dry_run: isDryRun
        };

        log((isDryRun ? '[演练开始]' : '[开始执行]') + ' 正在处理符合条件的文件...');
        const res = await fetch('/api/clean', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const data = await res.json();
        log(data.message);
        if (!isDryRun) {
            loadStats();
            loadHistory();
        }
    }

    async function scanDedup() {
        const minSize = document.getElementById('dedupMinSize').value;
        log('正在计算特征哈希并排查重复副本 (门槛 ' + minSize + ')...');
        const res = await fetch('/api/dedup_scan?min_size=' + minSize);
        const data = await res.json();
        const container = document.getElementById('dedupResults');
        if (data.actionable_groups_count === 0) {
            container.innerHTML = '<div style="color:var(--success); font-weight:600; margin-top:8px;">✓ 太棒了！未发现占用多份空间的重复文件。</div>';
            log('查重完成: 未发现冗余副本。');
            return;
        }
        container.innerHTML = `<div style="margin-top:12px; font-weight:600;">发现 ${data.actionable_groups_count} 组重复文件，共 ${data.total_wasted_copies} 个副本，可节省 ${data.total_saving_str} 物理空间！</div>`;
        log(`查重完成: 发现 ${data.actionable_groups_count} 组重复，可释放 ${data.total_saving_str}`);
    }

    async function executeDedup() {
        const minSize = document.getElementById('dedupMinSize').value;
        const action = document.getElementById('dedupAction').value;
        log('正在执行去重 (模式: ' + action + ')...');
        const res = await fetch('/api/dedup_exec', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ min_size: minSize, action: action })
        });
        const data = await res.json();
        log(data.message);
        loadStats();
        loadHistory();
    }

    async function loadWhitelist() {
        try {
            const res = await fetch('/api/whitelist');
            const data = await res.json();
            const tbody = document.getElementById('whitelistBody');
            tbody.innerHTML = '';
            if (!data.rules || data.rules.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-sub); padding:16px;">当前暂无白名单保护规则</td></tr>';
                return;
            }
            data.rules.forEach(r => {
                const protStr = r.protect === 'absolute' ? '<span style="color:var(--success); font-weight:600;">🔒 绝对保护</span>' : `<span style="color:var(--warning)">⏱️ 保留 ${r.retain_days} 天</span>`;
                const kwStr = r.keywords && r.keywords.length > 0 ? r.keywords.join(', ') : '-';
                const ts = (r.created_at || '').substring(0, 19).replace('T', ' ');
                tbody.innerHTML += `<tr>
                    <td><b>${r.name}</b></td>
                    <td><code>${r.wxid}</code></td>
                    <td>${protStr}</td>
                    <td>${kwStr}</td>
                    <td><small style="color:var(--text-sub)">${ts}</small></td>
                    <td><button class="btn btn-secondary" style="padding:4px 10px; font-size:12px;" onclick="removeWhitelistRule('${r.wxid}')">移除</button></td>
                </tr>`;
            });
        } catch (e) {}
    }

    async function addWhitelistRule() {
        const name = document.getElementById('wlName').value.trim();
        const wxid = document.getElementById('wlWxid').value.trim();
        if (!name || !wxid) {
            alert('请提供联系人姓名和微信号/群ID！');
            return;
        }
        const payload = {
            name: name,
            wxid: wxid,
            protect: document.getElementById('wlProtect').value,
            keywords: document.getElementById('wlKeywords').value.trim(),
            retain_days: parseInt(document.getElementById('wlRetainDays').value || '0')
        };
        const res = await fetch('/api/whitelist/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        log(data.message || '白名单已更新');
        document.getElementById('wlName').value = '';
        document.getElementById('wlWxid').value = '';
        document.getElementById('wlKeywords').value = '';
        loadWhitelist();
    }

    async function removeWhitelistRule(target) {
        if (!confirm('确定要移除规则 ' + target + ' 吗？')) return;
        const res = await fetch('/api/whitelist/remove', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target: target })
        });
        const data = await res.json();
        log(data.message || '规则已移除');
        loadWhitelist();
    }

    async function loadHistory() {
        try {
            const res = await fetch('/api/history');
            const data = await res.json();
            document.getElementById('histRuns').innerText = data.total_runs + ' 次';
            document.getElementById('histScansCleans').innerText = `扫描 ${data.total_scans} 次 / 清理 ${data.total_cleans} 次 / 去重 ${data.total_dedups} 次`;
            document.getElementById('histFreed').innerText = data.total_freed_str;
            document.getElementById('histProtected').innerText = data.total_protected_str;
            document.getElementById('histNps').innerText = data.nps_score !== null ? data.nps_score + ' / 10 分' : '尚未评分';
            document.getElementById('auditLogPath').innerText = data.audit_log_path || '';

            const tbody = document.getElementById('historyBody');
            tbody.innerHTML = '';
            const actMap = {
                'clean': '清理瘦身',
                'archive': '外置归档',
                'dedup_hardlink': 'APFS硬链接去重',
                'dedup_trash': '废纸篓去重',
                'scan': '空间扫描'
            };
            if (!data.history || data.history.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-sub); padding:16px;">尚无历史操作记录</td></tr>';
            } else {
                data.history.forEach(h => {
                    const ts = (h.timestamp || '').substring(0, 19).replace('T', ' ');
                    const actName = actMap[h.action] || h.action;
                    const freedStr = h.freed_bytes ? (h.freed_bytes / 1024 / 1024).toFixed(1) + ' MB' : '0 B';
                    const protStr = h.protected_bytes ? (h.protected_bytes / 1024 / 1024).toFixed(1) + ' MB' : '-';
                    tbody.innerHTML += `<tr>
                        <td><small style="color:var(--text-sub)">${ts}</small></td>
                        <td><b>${actName}</b></td>
                        <td>${h.count || 0}</td>
                        <td style="color:var(--success); font-weight:600;">${freedStr}</td>
                        <td style="color:var(--primary);">${protStr}</td>
                        <td><small style="color:var(--text-sub)">${h.note || ''}</small></td>
                    </tr>`;
                });
            }

            const alc = document.getElementById('auditLogConsole');
            if (data.recent_logs && data.recent_logs.length > 0) {
                alc.innerText = data.recent_logs.join('\\n');
            } else {
                alc.innerText = '> 审计日志文件尚为空或尚未生成操作。';
            }
            alc.scrollTop = alc.scrollHeight;
        } catch (e) {}
    }

    window.onload = loadStats;
</script>
</body>
</html>
"""


class WeChatSlimWebHandler(BaseHTTPRequestHandler):
    """本地轻量级 WebUI HTTP 请求处理器."""
    custom_path: Optional[Path] = None
    whitelist_config: Optional[Path] = None
    state_path: Optional[Path] = None

    def _send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/':
            raw = WEB_UI_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif parsed.path == '/api/stats':
            accounts = discover_accounts(self.custom_path)
            if not accounts:
                self._send_json({'error': '未找到微信账号目录'}, status=404)
                return
            acc = accounts[0]
            categories = scan_account(acc)
            total_bytes = sum(c.total_bytes for c in categories.values())
            cleanable_bytes = sum(c.total_bytes for k, c in categories.items() if not c.is_protected)
            clean_ratio = (cleanable_bytes / total_bytes * 100) if total_bytes > 0 else 0

            cat_dict = {}
            for k, c in categories.items():
                pct = (c.total_bytes / total_bytes * 100) if total_bytes > 0 else 0
                cat_dict[k] = {
                    'name': c.name,
                    'description': c.description,
                    'file_count': f'{c.file_count:,}',
                    'size_bytes': c.total_bytes,
                    'size_str': format_bytes(c.total_bytes),
                    'percent': f'{pct:.1f}',
                    'is_protected': c.is_protected,
                }

            self._send_json({
                'account': {
                    'id': acc.account_id,
                    'version': acc.version_type,
                    'root_path': str(acc.root_path),
                },
                'total_size_bytes': total_bytes,
                'total_size_str': format_bytes(total_bytes),
                'total_files': f'{sum(c.file_count for c in categories.values()):,}',
                'cleanable_size_bytes': cleanable_bytes,
                'cleanable_size_str': format_bytes(cleanable_bytes),
                'cleanable_ratio': f'{clean_ratio:.1f}',
                'categories': cat_dict,
            })
        elif parsed.path == '/api/dedup_scan':
            query = urllib.parse.parse_qs(parsed.query)
            min_size = query.get('min_size', ['500KB'])[0]
            accounts = discover_accounts(self.custom_path)
            if not accounts:
                self._send_json({'error': '未找到微信账号目录'}, status=404)
                return
            acc = accounts[0]
            categories = scan_account(acc)
            groups = find_duplicates(categories, ['video', 'file', 'attach'], min_size_bytes=parse_size_str(min_size))
            actionable = [g for g in groups if g.wasted_count > 0]
            total_saving = sum(g.saving_bytes for g in actionable)
            total_wasted = sum(g.wasted_count for g in actionable)

            self._send_json({
                'actionable_groups_count': len(actionable),
                'total_wasted_copies': total_wasted,
                'total_saving_bytes': total_saving,
                'total_saving_str': format_bytes(total_saving),
            })
        elif parsed.path == '/api/whitelist':
            wl_mgr = WhiteListManager(self.whitelist_config)
            rules = [r.to_dict() for r in wl_mgr.list_rules()]
            self._send_json({'rules': rules})
        elif parsed.path == '/api/history':
            state_mgr = StateManager(self.state_path)
            log_path = Path.home() / ".wechat_slim" / "audit.log"
            recent_logs = []
            if log_path.exists():
                try:
                    with open(log_path, 'r', encoding='utf-8') as lf:
                        recent_logs = [l.strip() for l in lf.readlines()[-30:]]
                except Exception:
                    pass
            self._send_json({
                'total_runs': state_mgr.total_runs,
                'total_scans': state_mgr.total_scans,
                'total_cleans': state_mgr.total_cleans,
                'total_dedups': state_mgr.total_dedups,
                'total_freed_bytes': state_mgr.total_freed_bytes,
                'total_freed_str': format_bytes(state_mgr.total_freed_bytes),
                'total_protected_bytes': state_mgr.total_protected_bytes,
                'total_protected_str': format_bytes(state_mgr.total_protected_bytes),
                'nps_score': state_mgr.nps_score,
                'history': [h.to_dict() for h in reversed(state_mgr.history[-20:])],
                'recent_logs': recent_logs,
                'audit_log_path': str(log_path),
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body = {}
        if length > 0:
            try:
                body = json.loads(self.rfile.read(length).decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json({'error': '无效的 JSON 请求体'}, status=400)
                return

        accounts = discover_accounts(self.custom_path)
        if not accounts:
            self._send_json({'error': '未找到微信账号目录'}, status=404)
            return
        acc = accounts[0]
        categories = scan_account(acc)

        if parsed.path == '/api/clean':
            days = int(body.get('days', 90))
            min_size = parse_size_str(body.get('min_size', '0B'))
            types = [t.strip() for t in body.get('types', 'video,file').split(',') if t.strip()]
            dry_run = bool(body.get('dry_run', True))
            archive_to = Path(body['archive_to']) if body.get('archive_to') else None
            wl_mgr = WhiteListManager(self.whitelist_config)

            res = execute_slimming(
                acc, categories, days, min_size, types, dry_run=dry_run, archive_to=archive_to, whitelist_mgr=wl_mgr
            )
            if not dry_run:
                state_mgr = StateManager(self.state_path)
                state_mgr.record_clean(
                    res.freed_count, res.freed_bytes, res.protected_count, res.protected_bytes, is_archive=bool(archive_to)
                )
                _audit_logger.info(
                    f"WebUI: executed clean freed={res.freed_count} ({res.freed_bytes} bytes), "
                    f"protected={res.protected_count} ({res.protected_bytes} bytes)"
                )
            msg = f"[演练完成] 预计影响 {res.freed_count:,} 个文件，可释放 {format_bytes(res.freed_bytes)} 空间" if dry_run else f"[处理完成] 成功处理 {res.freed_count:,} 个文件，释放 {format_bytes(res.freed_bytes)} 空间！"
            if res.protected_count > 0:
                msg += f" (已跳过锁定保护 {res.protected_count:,} 个核心人脉文件，{format_bytes(res.protected_bytes)})"
            self._send_json({
                'count': res.freed_count,
                'freed_bytes': res.freed_bytes,
                'freed_str': format_bytes(res.freed_bytes),
                'protected_count': res.protected_count,
                'protected_bytes': res.protected_bytes,
                'protected_str': format_bytes(res.protected_bytes),
                'message': msg,
            })
        elif parsed.path == '/api/dedup_exec':
            min_size = parse_size_str(body.get('min_size', '500KB'))
            action = body.get('action', 'hardlink')
            groups = find_duplicates(categories, ['video', 'file', 'attach'], min_size_bytes=min_size)
            actionable = [g for g in groups if g.wasted_count > 0]
            count, freed = execute_dedup(actionable, action=action, dry_run=False)
            state_mgr = StateManager(self.state_path)
            state_mgr.record_dedup(count, freed, action=action)
            _audit_logger.info(f"WebUI: executed dedup action={action}, processed={count}, freed={freed}")
            msg = f"[去重完成] 成功转换 {count:,} 个重复副本为 APFS 硬链接，物理释放 {format_bytes(freed)} 磁盘空间！" if action == 'hardlink' else f"[去重完成] 成功移入废纸篓 {count:,} 个重复副本，释放 {format_bytes(freed)} 空间！"
            self._send_json({'count': count, 'freed_bytes': freed, 'freed_str': format_bytes(freed), 'message': msg})
        elif parsed.path == '/api/whitelist/add':
            name = str(body.get('name', '')).strip()
            wxid = str(body.get('wxid', '')).strip()
            if not name or not wxid:
                self._send_json({'error': '名称与微信ID不能为空'}, status=400)
                return
            protect = body.get('protect', 'absolute')
            keywords = [k.strip() for k in str(body.get('keywords', '')).split(',') if k.strip()]
            retain_days = int(body.get('retain_days', 0))
            wl_mgr = WhiteListManager(self.whitelist_config)
            rule = wl_mgr.add(name, wxid, protect=protect, keywords=keywords, retain_days=retain_days)
            _audit_logger.info(f"WebUI: added whitelist rule '{rule.name}' ({rule.wxid})")
            self._send_json({'rule': rule.to_dict(), 'message': f'成功添加白名单规则: {rule.name}'})
        elif parsed.path == '/api/whitelist/remove':
            target = str(body.get('target', '')).strip()
            wl_mgr = WhiteListManager(self.whitelist_config)
            ok = wl_mgr.remove(target)
            if ok:
                _audit_logger.info(f"WebUI: removed whitelist rule '{target}'")
                self._send_json({'ok': True, 'message': f'已移除白名单规则: {target}'})
            else:
                self._send_json({'ok': False, 'message': f'未找到白名单规则: {target}'}, status=404)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        """静默默认 HTTP 请求日志，避免刷屏."""
        return


def cmd_web(args: argparse.Namespace) -> None:
    """启动本地轻量 WebUI 大盘."""
    port = getattr(args, 'port', 8080)
    custom_path = getattr(args, 'path', None)
    WeChatSlimWebHandler.custom_path = custom_path
    WeChatSlimWebHandler.whitelist_config = getattr(args, 'whitelist_config', None)
    WeChatSlimWebHandler.state_path = getattr(args, 'state_path', None)

    try:
        server = HTTPServer(('127.0.0.1', port), WeChatSlimWebHandler)
    except OSError as e:
        if e.errno == 48:
            print(f"{Colors.RED}[-] 启动失败: 本地端口 {port} 已被占用。{Colors.RESET}")
            print(f"    提示: 请使用 --port 指定其他端口，例如: wechat-slim web --port {port + 1}")
            return
        raise
    url = f"http://127.0.0.1:{port}"
    print('=' * 66)
    print('       WeChat Slim - 本地可视化图形大盘 (WebUI)')
    print('=' * 66)
    print(f'  • 网页服务已就绪: {url}')
    print('  • 按 Ctrl+C 可停止服务')
    print('-' * 66)

    if not getattr(args, 'no_browser', False):
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[✓] Web 服务已停止。')
        server.server_close()


