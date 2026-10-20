#!/usr/bin/env python3
"""把「AI 生图后自动去水印」写成全局规则，让之后每个会话都自动生效。

原理：WorkBuddy 会把用户级记忆文件（~/.workbuddy/user-*-personal/MEMORY.md）
注入到每一次会话。把一条受管规则写进去，模型在每次会话开始时就知道：
AI 生图之后必须自动去水印，不必等用户开口。

本脚本只维护一段被注释标记包裹的区块，不会改动记忆文件里的其他内容；
重复执行是幂等的，随时可以用 --uninstall 完整移除。

用法：
  python global_setup.py --status
  python global_setup.py --install
  python global_setup.py --install --dry-run
  python global_setup.py --uninstall
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

BEGIN = "<!-- ai-image-watermark-remover:begin -->"
END = "<!-- ai-image-watermark-remover:end -->"
BLOCK_RE = re.compile(
    re.escape(BEGIN) + r".*?" + re.escape(END),
    re.DOTALL,
)

SKILL_ROOT = Path(__file__).resolve().parents[1]
REMOVER = (SKILL_ROOT / "scripts" / "remove_watermark.py").as_posix()
SETUP = (SKILL_ROOT / "scripts" / "global_setup.py").as_posix()


def build_block() -> str:
    return "\n".join(
        [
            BEGIN,
            "## AI 生图自动去水印（全局规则，自动执行）",
            "",
            "- 【何时做】任何工具（图像生成、脚本绘图、设计稿导出等）产出了图片，"
            "在把图片交付、插入文档或展示给用户之前，一律先自动去水印。",
            "- 【怎么做】运行下面这条命令，不要先问用户要不要去水印：",
            f"  `python \"{REMOVER}\" <图片路径> --json`",
            "- 【范围】只处理 AI 生成的图片。用户自己提供的照片、截图、素材不要处理。",
            "- 【异常】命令报「已去过水印」说明处理过，直接继续；"
            "报失败或格式不支持，如实说明并附上原图路径。",
            "- 【维护】关闭该规则："
            f"`python \"{SETUP}\" --uninstall`",
            END,
        ]
    )


def candidate_memory_files() -> list[Path]:
    home = Path.home()
    base = home / ".workbuddy"
    out: list[Path] = []
    if base.is_dir():
        out.extend(sorted(base.glob("user-*-personal/MEMORY.md")))
        legacy = base / "MEMORY.md"
        if legacy.exists():
            out.append(legacy)
    # 同一台机器上可能有多套用户目录，取最近修改的那个
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    shutil.copystat(path, tmp) if path.exists() else None
    tmp.replace(path)


def status(path: Path) -> tuple[bool, bool]:
    """返回 (是否已安装, 块内路径是否指向当前技能)。

    技能升级后安装目录可能变化，旧块里的绝对路径会失效，必须能识别出来。
    """
    if not path.exists():
        return False, False
    text = read_text(path)
    m = BLOCK_RE.search(text)
    if not m:
        return False, False
    return True, REMOVER in m.group(0)


def install(path: Path, dry_run: bool) -> int:
    block = build_block()
    exists = path.exists()
    original = read_text(path) if exists else ""

    if BEGIN in original:
        new_text = BLOCK_RE.sub(block, original, count=1)
        action = "已更新"
    else:
        sep = "" if not original else ("\n" if original.endswith("\n") else "\n\n")
        new_text = original + sep + block + "\n"
        action = "已写入"

    if new_text == original:
        print(f"[=] 规则已是最新：{path}")
        return 0

    if dry_run:
        print(f"[预览] {path} 将{action}以下全局规则")
        print("-" * 60)
        print(block)
        print("-" * 60)
        return 0

    if exists:
        backup = path.with_suffix(path.suffix + ".wm-backup")
        if not backup.exists():
            shutil.copy2(path, backup)
            print(f"[i] 已备份原记忆文件到 {backup.name}")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)

    write_text(path, new_text)
    print(f"[✓] {action}全局规则：{path}")
    print("    之后每个新会话都会自动带上「AI 生图后去水印」的指令。")
    return 0


def uninstall(path: Path, dry_run: bool) -> int:
    if not path.exists():
        print(f"[i] 记忆文件不存在，无需卸载：{path}")
        return 0
    original = read_text(path)
    if BEGIN not in original:
        print(f"[i] 未发现本技能的规则块：{path}")
        return 0
    new_text = BLOCK_RE.sub("", original).strip("\n")
    if new_text:
        new_text += "\n"
    if dry_run:
        print(f"[预览] 将从 {path} 移除规则块")
        return 0
    write_text(path, new_text)
    print(f"[✓] 已移除全局规则：{path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="安装/卸载 AI 生图自动去水印的全局规则")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true", help="写入或更新全局规则")
    g.add_argument("--uninstall", action="store_true", help="移除全局规则")
    g.add_argument("--status", action="store_true", help="查看当前状态")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    parser.add_argument("--memory-file", help="手动指定用户级 MEMORY.md 路径")
    args = parser.parse_args()

    if args.memory_file:
        target = Path(args.memory_file).expanduser()
    else:
        candidates = candidate_memory_files()
        if not candidates:
            print(
                "[!] 没有找到 WorkBuddy 用户级记忆文件（~/.workbuddy/user-*-personal/MEMORY.md）。\n"
                "    请用 --memory-file 指定路径，或先启动一次 WorkBuddy 让它生成该文件。",
                file=sys.stderr,
            )
            return 2
        target = candidates[0]

    if args.status:
        if not target.exists():
            print(f"[状态] 记忆文件不存在：{target}")
            print("[状态] 全局规则：未安装")
            return 0
        installed, fresh = status(target)
        if not installed:
            state = "未安装"
        elif fresh:
            state = "已安装"
        else:
            state = "已安装，但规则里的脚本路径已过期（技能换过安装位置），请执行 --install 更新"
        print(f"[状态] 记忆文件：{target}")
        print(f"[状态] 全局规则：{state}")
        print(f"[状态] 去水印脚本：{REMOVER}")
        print(f"[状态] 脚本存在：{'是' if Path(REMOVER).exists() else '否'}")
        return 0

    if args.install:
        return install(target, args.dry_run)
    return uninstall(target, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
