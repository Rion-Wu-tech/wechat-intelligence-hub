#!/usr/bin/env python3
"""WeChat WhiteList Manager - 核心联系人与重要会话防删白名单系统.

负责核心人脉（家人、重要客户、重点工作群）的标签化管理与文件保护规则，
采用统一单向数据流与单一真实数据源 (Single Source of Truth)，
同时兼容 Contact / ProtectionLevel 与 WhiteListRule API。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import yaml
except ImportError:
    yaml = None


class ProtectionLevel(Enum):
    """保护级别枚举 (兼容 Contact 模型)."""
    ABSOLUTE = "absolute"      # 所有文件都保护
    FILES_ONLY = "files-only"  # 只保护文件，不保护视频


@dataclass
class Contact:
    """联系人数据模型 (面向对象视图)."""
    name: str
    wxid: str
    tags: List[str] = field(default_factory=list)
    protection: ProtectionLevel = ProtectionLevel.FILES_ONLY

    @classmethod
    def from_dict(cls, data: dict) -> Contact:
        prot_val = data.get('protection', 'files-only')
        if isinstance(prot_val, ProtectionLevel):
            prot_enum = prot_val
        else:
            try:
                prot_enum = ProtectionLevel(prot_val)
            except ValueError:
                prot_enum = ProtectionLevel.FILES_ONLY
        return cls(
            name=data.get('name', ''),
            wxid=data.get('wxid', ''),
            tags=data.get('tags', []),
            protection=prot_enum,
        )


@dataclass
class WhiteListConfig:
    """白名单配置 (兼容 Contact/Group 聚合视图)."""
    protected_contacts: List[Contact] = field(default_factory=list)
    auto_protected_groups: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'protected_contacts': [
                {
                    'name': c.name,
                    'wxid': c.wxid,
                    'tags': c.tags,
                    'protection': c.protection.value if isinstance(c.protection, ProtectionLevel) else str(c.protection),
                }
                for c in self.protected_contacts
            ],
            'auto_protected_groups': self.auto_protected_groups,
        }


@dataclass
class WhiteListRule:
    """白名单规则项 (核心单真实源数据模型)."""
    name: str                                           # 联系人或群聊名称 (如: "老婆", "重要客户A")
    wxid: str                                           # 微信 ID 或群聊 ID (如: "wxid_xxx", "xxx@chatroom")
    protect: str = "absolute"                           # 保护级别: "absolute" 或 "retain_days" 或 "files-only"
    keywords: List[str] = field(default_factory=list)   # 文件名关键词匹配 (如: ["合同", "宝宝", "结婚"])
    retain_days: int = 0                                # 当 protect 为 retain_days 时的有效天数 (0 为不限制)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    _wxid_lower: str = field(init=False, default="")
    _name_lower: str = field(init=False, default="")
    _keywords_lower: List[str] = field(init=False, default_factory=list)

    def __post_init__(self):
        self._wxid_lower = self.wxid.lower()
        self._name_lower = self.name.lower()
        self._keywords_lower = [kw.lower() for kw in self.keywords] if self.keywords else []

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WhiteListRule:
        return cls(
            name=data.get("name", ""),
            wxid=data.get("wxid", ""),
            protect=data.get("protect", "absolute"),
            keywords=data.get("keywords", data.get("tags", [])),
            retain_days=int(data.get("retain_days", 0)),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )

    def to_contact(self) -> Contact:
        prot_enum = ProtectionLevel.ABSOLUTE if self.protect in ["absolute", "retain_days"] else ProtectionLevel.FILES_ONLY
        return Contact(name=self.name, wxid=self.wxid, tags=self.keywords, protection=prot_enum)


class WhiteListManager:
    """精简统一白名单管理器.

    使用规则字典作为全局单一数据源 (Single Source of Truth)，
    所有 Contact API 与 Rule API 均直接对单一数据源操作，消除双轨同步冗余。
    """

    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        if config_path:
            self.config_path = Path(config_path).expanduser().resolve()
        else:
            self.config_path = Path.home() / ".wechat_slim" / "config" / "whitelist.yaml"

        self.rules: Dict[str, WhiteListRule] = {}
        self.name_index: Dict[str, str] = {}
        self.auto_protected_groups: List[str] = []
        self._config: WhiteListConfig = WhiteListConfig()
        self._format_is_contact_yaml = False
        self.load()

    def load(self) -> WhiteListConfig:
        """从配置文件加载白名单 (支持 YAML 与 JSON)."""
        self.rules.clear()
        self.name_index.clear()
        self.auto_protected_groups.clear()
        self._config = WhiteListConfig()

        target_file = self.config_path
        if not target_file.exists():
            fallback_json = Path.home() / ".wechat_slim_whitelist.json"
            if fallback_json.exists():
                target_file = fallback_json
            else:
                return self._config

        try:
            content = target_file.read_text(encoding="utf-8")
            data: Dict[str, Any] = {}

            if target_file.suffix in [".yaml", ".yml"] and yaml is not None:
                data = yaml.safe_load(content) or {}
            else:
                try:
                    data = json.loads(content)
                except Exception:
                    if yaml is not None:
                        data = yaml.safe_load(content) or {}

            # 1. 优先解析 rules 格式
            for r_data in data.get("rules", []):
                rule = WhiteListRule.from_dict(r_data)
                self._upsert_rule_internal(rule.name, rule.wxid, rule.protect, rule.keywords, rule.retain_days, rule.created_at)

            # 2. 解析 protected_contacts 格式
            if "protected_contacts" in data or "auto_protected_groups" in data:
                self._format_is_contact_yaml = True
                self.auto_protected_groups = data.get("auto_protected_groups", [])
                contacts = []
                for c_data in data.get("protected_contacts", []):
                    c = Contact.from_dict(c_data)
                    contacts.append(c)
                    key = c.wxid.strip().lower()
                    if key not in self.rules:
                        prot_str = "absolute" if c.protection == ProtectionLevel.ABSOLUTE else "files-only"
                        self._upsert_rule_internal(c.name, c.wxid, prot_str, c.tags)
                self._config.protected_contacts = contacts
                self._config.auto_protected_groups = list(self.auto_protected_groups)
            else:
                self._config.protected_contacts = [r.to_contact() for r in self.rules.values()]

        except Exception:
            self.rules.clear()
            self.name_index.clear()
            self.auto_protected_groups.clear()
            self._config = WhiteListConfig()

        return self._config

    def _upsert_rule_internal(
        self,
        name: str,
        wxid: str,
        protect: str = "absolute",
        keywords: Optional[List[str]] = None,
        retain_days: int = 0,
        created_at: Optional[str] = None,
    ) -> WhiteListRule:
        if not wxid or not wxid.strip():
            raise ValueError("wxid 不能为空")
        clean_wxid = wxid.strip()
        clean_name = name.strip() or clean_wxid
        key = clean_wxid.lower()
        rule = WhiteListRule(
            name=clean_name,
            wxid=clean_wxid,
            protect=protect,
            keywords=[k.strip() for k in (keywords or []) if k.strip()],
            retain_days=retain_days,
            created_at=created_at or datetime.now().isoformat(),
        )
        self.rules[key] = rule
        self.name_index[clean_name.lower()] = key
        return rule

    def save(self, config: Optional[WhiteListConfig] = None) -> None:
        """持久化保存白名单 (单源写回)."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            target_cfg = config if config is not None else self._config

            if target_cfg is not None:
                self.auto_protected_groups = list(target_cfg.auto_protected_groups)
                # 仅将新加入 protected_contacts 的联系人注册为 rule，不覆盖已有 rule 的属性
                for c in target_cfg.protected_contacts:
                    key = c.wxid.strip().lower()
                    if key not in self.rules:
                        prot_str = "absolute" if c.protection == ProtectionLevel.ABSOLUTE else "files-only"
                        self._upsert_rule_internal(c.name, c.wxid, prot_str, c.tags)
                self._config = target_cfg
            else:
                target_cfg = WhiteListConfig(
                    protected_contacts=[r.to_contact() for r in self.rules.values()],
                    auto_protected_groups=list(self.auto_protected_groups),
                )
                self._config = target_cfg

            if self._format_is_contact_yaml or self.config_path.suffix in [".yaml", ".yml"]:
                out_data = target_cfg.to_dict()
                if self.rules:
                    out_data["rules"] = [r.to_dict() for r in self.rules.values()]
                if self.config_path.suffix in [".yaml", ".yml"] and yaml is not None:
                    with open(self.config_path, "w", encoding="utf-8") as f:
                        yaml.dump(out_data, f, allow_unicode=True)
                else:
                    with open(self.config_path, "w", encoding="utf-8") as f:
                        json.dump(out_data, f, ensure_ascii=False, indent=2)
                print(f"[✓] 白名单配置已保存到：{self.config_path}")
            else:
                data = {
                    "version": "1.0",
                    "updated_at": datetime.now().isoformat(),
                    "rules": [r.to_dict() for r in self.rules.values()],
                }
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # --- 统一核心规则 API ---
    def add(
        self,
        name: str,
        wxid: str,
        protect: str = "absolute",
        keywords: Optional[List[str]] = None,
        retain_days: int = 0,
    ) -> WhiteListRule:
        """添加或更新规则."""
        rule = self._upsert_rule_internal(name, wxid, protect, keywords, retain_days)
        # 同步更新 _config.protected_contacts
        for idx, c in enumerate(self._config.protected_contacts):
            if c.wxid == wxid:
                self._config.protected_contacts[idx] = rule.to_contact()
                break
        else:
            self._config.protected_contacts.append(rule.to_contact())
        self.save()
        return rule

    def remove(self, identifier: str) -> bool:
        """按 wxid 或名称移除规则."""
        target_key = identifier.strip().lower()
        key_to_remove = self.name_index.get(target_key, target_key)
        if key_to_remove in self.rules:
            rule = self.rules.pop(key_to_remove)
            self.name_index.pop(rule.name.lower(), None)
            self._config.protected_contacts = [c for c in self._config.protected_contacts if c.wxid.lower() != key_to_remove]
            self.save()
            return True
        return False

    def get(self, identifier: str) -> Optional[WhiteListRule]:
        """按 wxid 或名称获取规则."""
        target_key = identifier.strip().lower()
        return self.rules.get(self.name_index.get(target_key, target_key))

    def list_rules(self) -> List[WhiteListRule]:
        """列出所有规则."""
        return list(self.rules.values())

    def clear(self) -> None:
        """清空所有白名单."""
        self.rules.clear()
        self.name_index.clear()
        self.auto_protected_groups.clear()
        self._config = WhiteListConfig()
        self.save()

    # --- Contact / Group 兼容层 API (轻量委托) ---
    def add_contact(
        self,
        name: str,
        wxid: str,
        tags: Optional[List[str]] = None,
        protection: ProtectionLevel = ProtectionLevel.FILES_ONLY,
    ) -> None:
        """添加联系人 (委托给单一真实源)."""
        prot_str = "absolute" if protection == ProtectionLevel.ABSOLUTE else "files-only"
        is_update = any(c.wxid == wxid for c in self._config.protected_contacts)
        self.add(name=name, wxid=wxid, protect=prot_str, keywords=tags)
        self._format_is_contact_yaml = True
        if is_update:
            print(f"[✓] 已更新联系人：{name} ({wxid})")
        else:
            print(f"[✓] 已添加联系人：{name} ({wxid})")

    def remove_contact(self, wxid: str) -> bool:
        """移除联系人."""
        ok = self.remove(wxid)
        if ok:
            print(f"[✓] 已从白名单移除：{wxid}")
        else:
            print(f"[-] 未找到联系人：{wxid}")
        return ok

    def list_contacts(self) -> List[Contact]:
        """获取联系人列表."""
        return list(self._config.protected_contacts)

    def get_protection_level(self, wxid_or_name: str) -> Optional[ProtectionLevel]:
        """获取保护级别."""
        for c in self._config.protected_contacts:
            if c.wxid == wxid_or_name or c.name == wxid_or_name:
                return c.protection
        rule = self.get(wxid_or_name)
        if rule:
            return ProtectionLevel.ABSOLUTE if rule.protect in ["absolute", "retain_days"] else ProtectionLevel.FILES_ONLY
        return None

    def is_group_protected(self, group_name: str) -> bool:
        """检查群聊是否受保护."""
        for g in self.auto_protected_groups:
            if group_name == g or group_name.startswith(g):
                return True
        return False

    # --- 防删判断核心逻辑 ---
    def is_protected(
        self,
        target: Union[str, Path],
        mtime: Optional[float] = None,
    ) -> Union[bool, Tuple[bool, Optional[str]]]:
        """统一防删判断 (根据入参自动区分联系人检查与文件路径检查)."""
        # 1. 联系人/群名称判断 (返回 bool)
        if mtime is None and isinstance(target, str) and not ("/" in target or "\\" in target):
            for c in self._config.protected_contacts:
                if c.wxid == target or c.name == target:
                    return True
            target_key = target.strip().lower()
            return target_key in self.rules or target_key in self.name_index or self.is_group_protected(target)

        # 2. 文件路径与修改时间判断 (返回 (bool, reason))
        if not self.rules and not self._config.protected_contacts:
            return False, None

        if isinstance(target, Path):
            path_str = str(target).lower()
            filename = target.name.lower()
            parts = [p.lower() for p in target.parts]
        else:
            path_str = str(target).lower()
            filename = os.path.basename(path_str)
            parts = path_str.replace('\\', '/').split('/')

        needs_timestamp = any(r.protect == "retain_days" and r.retain_days > 0 for r in self.rules.values())
        now_ts = datetime.now().timestamp() if needs_timestamp else 0.0
        file_mtime = mtime if mtime is not None else 0.0

        for rule in self.rules.values():
            r_wxid = getattr(rule, '_wxid_lower', '') or rule.wxid.lower()
            r_name = getattr(rule, '_name_lower', '') or rule.name.lower()
            r_kws = getattr(rule, '_keywords_lower', None)
            if r_kws is None:
                r_kws = [k.lower() for k in rule.keywords]

            # 安全约束: wxid 只允许"精确路径段"匹配 (目录名完全相等)。
            # 严禁对完整路径做子串匹配 —— 微信 4.0 的账号根目录本身就叫
            # "<wxid>_<序号>" (例如 wxid_kdm0jksur2yh12_6804)，一旦用子串匹配，
            # 任意 wxid 规则都会命中该账号下 100% 的文件，导致白名单彻底失真
            # (表现: 报告显示"保护了 5.4GB"但实际什么都没保护，或反过来保护一切)。
            matched = (
                r_wxid in parts
                or (r_name and (r_name in parts or r_name in filename))
                or (r_kws and any(kw in filename for kw in r_kws))
            )

            if matched:
                if rule.protect in ["absolute", "files-only"]:
                    return True, f"白名单保护: {rule.name} ({rule.wxid}) [绝对保护]"
                elif rule.protect == "retain_days" and rule.retain_days > 0:
                    file_age_days = (now_ts - file_mtime) / 86400 if file_mtime > 0 else 0
                    if file_age_days <= rule.retain_days:
                        return True, f"白名单保护: {rule.name} ({rule.wxid}) [保留 {rule.retain_days} 天内文件]"
                    else:
                        # 超过保留天数，不保护
                        continue

        return False, None
