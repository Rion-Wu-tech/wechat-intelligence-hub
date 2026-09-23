---
name: wechat-slim
description: Safe WeChat storage profiling, deduplication via APFS hardlinks, VIP contact protection, non-destructive archiving, and storage slimming. Use whenever the user asks to check WeChat storage space, clean WeChat cache/videos/files, find and hardlink duplicate files, manage protected contacts/groups, or view lifetime slimming statistics.
---

# WeChat Slim Agent Skill

WeChat Slim (`wechat-slim`) provides 100% text message-safe storage profiling, APFS hardlink deduplication, VIP whitelist isolation, and non-destructive external archiving for macOS and PC WeChat.

Default script entrypoint:

```bash
"skills/wechat-slim/scripts/slim.sh" <subcommand> [options]
```

## Safety Guarantees (Strict Rules)

1. **Absolute Database Lock**: Never touch, modify, or delete `db_storage`, `*.db`, `*.db-wal`, `*.sqlite`, or `*.wcdb`. Core chat texts and message records remain 100% safe.
2. **Safe Deletion / Trash Default**: Files are moved to the macOS system Trash by default rather than permanently deleted (`rm -rf`), allowing easy Finder restoration.
3. **APFS Hardlink Deduplication**: Duplicate files across multiple chat groups are deduplicated via atomic APFS hardlinks. All WeChat chat windows still open the files normally, but they consume only 1 copy on the physical SSD.
4. **VIP Whitelist Isolation**: Contacts and chat rooms in the whitelist (e.g. family, boss, major clients) are strictly skipped and locked against deletion.

## Standard Scenarios & Intent Mapping

When the user interacts via natural language, map their request to the corresponding command:

### 1. Storage Profiling & Inspection
- **User Intent**: "帮我看看微信占了多少空间", "微信占用大不大", "微信里什么最占地方"
- **Action**: Run storage scan
```bash
skills/wechat-slim/scripts/slim.sh scan
```

### 2. Space Slimming & Cleanup
- **User Intent**: "帮我清理微信大文件", "把半年前的视频清掉", "微信清理瘦身"
- **Action**: Always run with `--dry-run` first to preview the impact, or execute directly if explicitly confirmed:
```bash
# Preview 90 days old videos and files larger than 10MB
skills/wechat-slim/scripts/slim.sh clean --days 90 --min-size 10MB --types video,file --dry-run

# Actual execution
skills/wechat-slim/scripts/slim.sh clean --days 90 --min-size 10MB --types video,file -f
```

### 3. Multi-Group Duplicate Deduplication
- **User Intent**: "多群转发的文件太占地方了", "微信文件查重", "帮我去重"
- **Action**: APFS hardlink deduplication
```bash
skills/wechat-slim/scripts/slim.sh dedup --action hardlink -f
```

### 4. VIP Whitelist Protection
- **User Intent**: "把老婆加入防删名单", "千万别删领导发给我的合同", "查看白名单"
- **Action**: Whitelist tag management
```bash
# Add absolute protection rule
skills/wechat-slim/scripts/slim.sh tag --add "老婆" --wxid wxid_family --protect absolute

# Add keyword protection
skills/wechat-slim/scripts/slim.sh tag --add "核心客户" --wxid xxx@chatroom --keywords "合同,发票,协议"

# List active protection rules
skills/wechat-slim/scripts/slim.sh tag --list
```

### 5. External Drive / NAS Non-destructive Archiving
- **User Intent**: "我想把微信以前的老文件转存到移动硬盘里", "归档到外置SSD"
- **Action**: Use `--archive-to`
```bash
skills/wechat-slim/scripts/slim.sh clean --days 180 --archive-to "/Volumes/MyPassport/WeChatArchive" -f
```

### 6. Local Graphical Dashboard (WebUI)
- **User Intent**: "打开微信瘦身网页大盘", "我想在浏览器里看图表操作"
- **Action**: Launch WebUI server
```bash
skills/wechat-slim/scripts/slim.sh web --port 8080
```

### 7. Lifetime Statistics & Audit Log
- **User Intent**: "查看我累计清理了多少空间", "查看历史操作记录"
- **Action**: Query lifetime metrics
```bash
skills/wechat-slim/scripts/slim.sh stats
```
