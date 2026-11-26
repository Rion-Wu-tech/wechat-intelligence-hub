# Release Notes - WeChat Slim v1.0.0 (Official MVP)

**发布日期**: 2026-09-07  
**版本代号**: `v1.0.0-gold`  
**适用平台**: macOS (Apple Silicon M1/M2/M3/M4 & Intel x86_64)  
**Python 要求**: Python >= 3.8

---

## 🌟 核心突破与亮点 (Key Highlights)

### 1. ⚡ APFS 秒级硬链接去重 (`wechat-slim dedup`)
- 专为 macOS APFS 文件系统设计，解决微信“一个文件转发到多个群聊占用翻倍”的长期痛点。
- 去重后多份文件共享同一磁盘 Inode，**立省数十 GB 空间，微信会话内无需重新下载、点击即开**。
- 支持演练模式 (`--dry-run`) 预估释放空间与节省统计。

### 2. 🛡️ VIP 核心人脉防删白名单 (`wechat-slim tag`)
- 支持为老婆、孩子、老板、重要商务伙伴设定多层级防删规则。
- 支持 `absolute`（绝对豁免）与关键词凭证拦截（如“合同、协议、发票”）。
- 清理引擎具备一票否决权：命中白名单的数据绝不被移入废纸篓或归档。

### 3. 📦 废纸篓安全清理与外置硬盘无损归档 (`wechat-slim clean`)
- 默认删除接入 macOS 废纸篓（非 `rm -rf`），可随时在 Finder 中无损放回原处。
- 创新性提供 `--archive-to` 选项，可将超期大视频与文件一键迁移至外接 SSD / NAS，原目录结构完整保留。

### 4. 🔒 数据库绝对防护防线 (Database Safeguard)
- 核心 SQLite/WCDB 数据库、联系人库与索引在代码执行层被强力锁定隔离，全流程 100% 免死金牌。

### 5. 纯原生零依赖 (Zero-Dependency)
- 核心命令行程序、扫描、去重、清理、统计及 WebUI 100% 仅使用 Python 标准库。
- 用户无需配置复杂环境或解决 pip 冲突，开箱即用。

### 6. 🤖 深度原生 Agent Skill 支持
- 随包发布 `skills/wechat-slim/SKILL.md`，即插即用于 Antigravity、Claude Code、Cursor 与 Codex 等下一代 AI 编程助理。

---

## 🧪 质量与测试报告

- **自动化测试**: 164 项单元测试与集成测试，覆盖率 > 90%，100% PASS。
- **真实验证**: 在 macOS 微信 4.0 与 3.x 真实容器中完成全流程清理，单机实测释放 5.4GB~40GB+ 冗余。

---

## 📦 安装与升级

```bash
# 从源码安装
git clone https://github.com/LuckTerence/CleanYourWechatTool.git
cd CleanYourWechatTool
pip install .

# 验证安装
wechat-slim --help
```
