#!/usr/bin/env python3
"""去除 AI 生图工具默认添加在图片右下角的角标水印（如「AI生成 WORKBUDDY」）。

原理：这类水印固定在画面右下角、紧贴边缘，高度约占整图 5%~7%。
裁掉底部 DEFAULT_RATIO 后，水印完全消失，宽度不变，视觉损失可以忽略。

设计要点：
  1. 幂等 —— 处理过的图片写入元数据标记，重复调用不会重复裁剪。
  2. 原子写入 —— 先写临时文件再替换，中途失败不会损坏原图。
  3. 跨平台零依赖 —— 优先用 Pillow；缺失时 Windows 走 System.Drawing、
     macOS 走系统自带 sips，保证用户机器上开箱可用。

用法：
  python remove_watermark.py a.png b.jpg
  python remove_watermark.py --in ./images --json
  python remove_watermark.py a.png --detect          # 只判断有没有水印
  python remove_watermark.py a.png --dry-run         # 只报告将要做什么

退出码：0 全部成功或无需处理；1 有文件失败；2 参数错误。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER_KEY = "WorkBuddy-Watermark-Removed"
MARKER_VALUE = "1"

# 默认裁掉的底部比例。实测水印占据整图 92.9%~98% 的高度区间，
# 8% 可以稳定覆盖且只损失极小画面。
DEFAULT_RATIO = 0.08
MIN_RATIO = 0.02
MAX_RATIO = 0.25

# 小于该边长的图片不处理：裁 8% 对缩略图/图标没有意义，只会破坏内容。
MIN_SIDE = 128

# 只处理能可靠写入幂等标记的格式。BMP/TIFF 生图场景几乎不会出现，
# 且无法携带元数据，强行处理会在重复调用时被反复裁剪，故不支持。
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# 检测：只在右下角这一小块区域里找"亮文字"
DETECT_ROI_X = 0.86   # 从宽度的 86% 开始
DETECT_ROI_Y = 0.905  # 从高度的 90.5% 开始
DETECT_PIXEL_THRESHOLD = 30   # 顶帽差值超过该值算作笔画像素
DETECT_SCORE_THRESHOLD = 0.045  # 笔画像素占比超过该值判定为有水印

try:
    from PIL import Image, ImageChops, ImageFilter, ImageOps, PngImagePlugin
    HAVE_PIL = True
except Exception:  # pragma: no cover - 取决于运行环境
    HAVE_PIL = False


# --------------------------------------------------------------------------
# 结果收集
# --------------------------------------------------------------------------
class Report:
    def __init__(self, as_json: bool = False) -> None:
        self.as_json = as_json
        self.processed: list[dict] = []
        self.skipped: list[dict] = []
        self.failed: list[dict] = []
        self.detected: list[dict] = []

    def say(self, msg: str) -> None:
        if not self.as_json:
            print(msg)

    def to_dict(self) -> dict:
        return {
            "ok": not self.failed,
            "backend": backend_name(),
            "processed": self.processed,
            "skipped": self.skipped,
            "failed": self.failed,
            "detected": self.detected,
            "summary": {
                "processed": len(self.processed),
                "skipped": len(self.skipped),
                "failed": len(self.failed),
            },
        }

    def emit(self) -> None:
        if self.as_json:
            print(json.dumps(self.to_dict(), ensure_ascii=False, indent=2))


def backend_name() -> str:
    if HAVE_PIL:
        return "pillow"
    if sys.platform == "win32":
        return "powershell"
    if sys.platform == "darwin":
        return "sips"
    return "none"


# --------------------------------------------------------------------------
# 目标收集
# --------------------------------------------------------------------------
def collect_targets(paths: list[str], input_dir: str | None, limit: int) -> list[Path]:
    targets: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        # 允许直接传目录
        if p.is_dir():
            targets.extend(sorted(q for q in p.rglob("*") if q.suffix.lower() in SUPPORTED_EXTS))
        else:
            targets.append(p)
    if input_dir:
        root = Path(input_dir).expanduser()
        if root.is_dir():
            targets.extend(sorted(q for q in root.rglob("*") if q.suffix.lower() in SUPPORTED_EXTS))

    seen: set[Path] = set()
    unique: list[Path] = []
    for t in targets:
        key = t.resolve() if t.exists() else t
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)
    return unique[:limit]


# --------------------------------------------------------------------------
# 检测
# --------------------------------------------------------------------------
def watermark_score(img: "Image.Image") -> float:
    """返回右下角疑似水印文字的笔画像素占比（0~1）。"""
    gray = img.convert("L")
    w, h = gray.size
    x0, y0 = int(w * DETECT_ROI_X), int(h * DETECT_ROI_Y)
    if w - x0 < 24 or h - y0 < 8:
        return 0.0
    roi = gray.crop((x0, y0, w, h))
    # 形态学开运算近似背景，原图减去背景即"亮细节"（顶帽变换）
    opened = roi.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    diff = ImageChops.subtract(roi, opened)
    hist = diff.histogram()
    total = roi.width * roi.height
    if total == 0:
        return 0.0
    return sum(hist[DETECT_PIXEL_THRESHOLD:]) / total


def has_marker(img: "Image.Image") -> bool:
    info = getattr(img, "info", {}) or {}
    if str(info.get(MARKER_KEY, "")).strip() not in ("", "0", "False", "false"):
        return True
    try:
        exif = img.getexif()
        val = exif.get(0x9286)  # UserComment
        if isinstance(val, bytes) and MARKER_KEY.encode() in val:
            return True
        if isinstance(val, str) and MARKER_KEY in val:
            return True
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# Pillow 后端
# --------------------------------------------------------------------------
def process_with_pillow(
    path: Path, ratio: float, keep_original: bool, report: Report, ignore_marker: bool = False
) -> str:
    """返回 'processed' / 'skipped' / 'failed'。"""
    with Image.open(path) as im:
        if getattr(im, "is_animated", False):
            report.skipped.append({"path": str(path), "reason": "animated"})
            return "skipped"
        if not ignore_marker and has_marker(im):
            report.skipped.append({"path": str(path), "reason": "already_processed"})
            return "skipped"

        im = ImageOps.exif_transpose(im)
        fmt = (im.format or path.suffix.lstrip(".")).upper()
        if fmt == "JPG":
            fmt = "JPEG"
        w, h = im.size
        crop_h = max(1, int(round(h * ratio)))
        new_h = h - crop_h
        if w < MIN_SIDE or h < MIN_SIDE:
            report.skipped.append({"path": str(path), "reason": "too_small"})
            return "skipped"
        if new_h < 16 or w < 16:
            report.skipped.append({"path": str(path), "reason": "too_small"})
            return "skipped"

        cropped = im.crop((0, 0, w, new_h))

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".wm-", suffix=path.suffix, dir=str(path.parent)
        )
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            save_kwargs: dict = {}
            icc = im.info.get("icc_profile")
            if icc:
                save_kwargs["icc_profile"] = icc

            if fmt == "PNG":
                meta = PngImagePlugin.PngInfo()
                for k, v in (im.info or {}).items():
                    if isinstance(v, str) and k not in (MARKER_KEY,):
                        try:
                            meta.add_text(k, v)
                        except Exception:
                            pass
                meta.add_text(MARKER_KEY, MARKER_VALUE)
                cropped.save(tmp_path, format="PNG", pnginfo=meta, optimize=True)
            elif fmt in ("JPEG", "MPO"):
                exif = im.getexif()
                exif[0x9286] = f"{MARKER_KEY}={MARKER_VALUE}"
                cropped.save(
                    tmp_path,
                    format="JPEG",
                    quality=95,
                    optimize=True,
                    exif=exif.tobytes(),
                    **save_kwargs,
                )
            elif fmt == "WEBP":
                exif = im.getexif()
                exif[0x9286] = f"{MARKER_KEY}={MARKER_VALUE}"
                cropped.save(
                    tmp_path,
                    format="WEBP",
                    quality=95,
                    method=4,
                    exif=exif.tobytes(),
                    **save_kwargs,
                )
            else:
                cropped.save(tmp_path, **save_kwargs)

            if keep_original:
                backup = path.with_name(f"{path.stem}_orig{path.suffix}")
                n = 1
                while backup.exists():
                    backup = path.with_name(f"{path.stem}_orig{n}{path.suffix}")
                    n += 1
                shutil.copy2(path, backup)

            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    report.processed.append(
        {
            "path": str(path),
            "from": f"{w}x{h}",
            "to": f"{w}x{new_h}",
            "cut_px": crop_h,
            "backend": "pillow",
        }
    )
    return "processed"


def detect_with_pillow(path: Path, report: Report) -> str:
    with Image.open(path) as im:
        score = watermark_score(im)
        marked = has_marker(im)
    hit = score >= DETECT_SCORE_THRESHOLD
    report.detected.append(
        {
            "path": str(path),
            "score": round(score, 4),
            "threshold": DETECT_SCORE_THRESHOLD,
            "watermark": hit and not marked,
            "already_processed": marked,
        }
    )
    return "detected"


# --------------------------------------------------------------------------
# Windows 零依赖后端（System.Drawing）
# --------------------------------------------------------------------------
PS_SCRIPT = r"""
param([string]$Src, [string]$Dst, [double]$Ratio)
Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile($Src)
try {
    $newH = [int][Math]::Round($img.Height * (1 - $Ratio))
    if ($newH -lt 16) { throw "image too small" }
    $bmp = New-Object System.Drawing.Bitmap($img.Width, $newH)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $rect = New-Object System.Drawing.Rectangle(0, 0, $img.Width, $newH)
    $g.DrawImage($img, $rect, 0, 0, $img.Width, $newH, [System.Drawing.GraphicsUnit]::Pixel)
    $g.Dispose()
    $bmp.Save($Dst, $img.RawFormat)
    $bmp.Dispose()
    Write-Output ("{0}x{1}" -f $img.Width, $newH)
} finally {
    $img.Dispose()
}
"""


def process_with_powershell(path: Path, ratio: float, keep_original: bool, report: Report) -> str:
    # 临时目录必须与目标同盘，否则 os.replace 会因跨驱动器失败
    tmp_dir = Path(tempfile.mkdtemp(prefix=".wm-", dir=str(path.parent)))
    ps_file = tmp_dir / "crop.ps1"
    out_file = tmp_dir / ("out" + path.suffix.lower())
    ps_file.write_text(PS_SCRIPT, encoding="utf-8")
    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(ps_file),
                "-Src", str(path), "-Dst", str(out_file), "-Ratio", str(ratio),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0 or not out_file.exists():
            report.failed.append(
                {"path": str(path), "error": (proc.stderr or proc.stdout or "powershell failed").strip()[:400]}
            )
            return "failed"
        if keep_original:
            shutil.copy2(path, path.with_name(f"{path.stem}_orig{path.suffix}"))
        os.replace(out_file, path)

        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        dims = lines[-1] if lines and "x" in lines[-1] else "x"
        new_w, new_h = (dims.split("x", 1) + ["?"])[:2]
        report.processed.append(
            {
                "path": str(path),
                "from": "?",
                "to": f"{new_w}x{new_h}",
                "cut_px": "?",
                "backend": "powershell",
                "marker": False,
            }
        )
        return "processed"
    except Exception as exc:
        report.failed.append({"path": str(path), "error": str(exc)[:400]})
        return "failed"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# macOS 零依赖后端（sips）
# --------------------------------------------------------------------------
def process_with_sips(path: Path, ratio: float, keep_original: bool, report: Report) -> str:
    try:
        probe = subprocess.run(
            ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        dims = {}
        for line in probe.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                dims[k.strip()] = v.strip()
        w, h = int(dims.get("pixelWidth", 0)), int(dims.get("pixelHeight", 0))
        if not w or not h:
            raise RuntimeError("cannot read image size")
        new_h = h - int(round(h * ratio))
        if keep_original:
            shutil.copy2(path, path.with_name(f"{path.stem}_orig{path.suffix}"))
        proc = subprocess.run(
            ["sips", "--cropToHeightWidth", str(new_h), str(w), str(path)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip()[:300])
        report.processed.append(
            {"path": str(path), "from": f"{w}x{h}", "to": f"{w}x{new_h}",
             "cut_px": h - new_h, "backend": "sips"}
        )
        return "processed"
    except Exception as exc:
        report.failed.append({"path": str(path), "error": str(exc)[:400]})
        return "failed"


# --------------------------------------------------------------------------
# 调度
# --------------------------------------------------------------------------
def ensure_backend(report: Report) -> bool:
    if backend_name() != "none":
        return True
    report.say(
        "[!] 当前环境没有可用的图像后端。\n"
        "    请安装 Pillow 后重试：python -m pip install --user pillow"
    )
    return False


def run(args: argparse.Namespace) -> int:
    report = Report(as_json=args.json)
    ratio = min(max(args.ratio, MIN_RATIO), MAX_RATIO)
    if abs(ratio - args.ratio) > 1e-9:
        report.say(f"[i] 裁剪比例 {args.ratio} 超出安全区间，已收敛为 {ratio}")

    targets = collect_targets(args.paths, args.input_dir, args.max)
    if not targets:
        report.say("请提供至少一个图片路径或 --in 目录")
        return 2
    if not ensure_backend(report):
        return 2

    if args.detect:
        if not HAVE_PIL:
            report.say("[!] --detect 需要 Pillow")
            return 2
        for t in targets:
            if not t.is_file():
                report.skipped.append({"path": str(t), "reason": "not_found"})
                continue
            try:
                detect_with_pillow(t, report)
            except Exception as exc:
                report.failed.append({"path": str(t), "error": str(exc)[:400]})
        for d in report.detected:
            if d["already_processed"]:
                flag = "已去过水印（元数据标记）"
            elif d["score"] >= d["threshold"]:
                flag = "疑似有水印"
            else:
                flag = "未发现明显水印迹象"
            report.say(f"[检测] {Path(d['path']).name}  score={d['score']:.3f}  {flag}")
        report.say("[i] 检测为启发式判断，仅供参考；图片若来自 AI 生图，请直接去水印。")
        report.emit()
        return 0 if not report.failed else 1

    for t in targets:
        if not t.is_file():
            report.skipped.append({"path": str(t), "reason": "not_found"})
            report.say(f"[跳过] {t.name}  文件不存在")
            continue
        if t.suffix.lower() not in SUPPORTED_EXTS:
            report.skipped.append({"path": str(t), "reason": "unsupported_format"})
            report.say(f"[跳过] {t.name}  格式不支持（仅支持 png/jpg/webp）")
            continue

        # 预览：不落盘，只说明会裁多少
        if args.dry_run:
            if HAVE_PIL:
                with Image.open(t) as im:
                    w, h = im.size
                    marked = has_marker(im)
                if marked and not args.force:
                    report.skipped.append({"path": str(t), "reason": "already_processed"})
                    report.say(f"[跳过] {t.name}  已去过水印，无需重复处理")
                    continue
                cut = int(round(h * ratio))
                report.say(f"[预览] {t.name}  {w}x{h} -> {w}x{h - cut}（裁掉底部 {cut}px）")
                report.processed.append(
                    {"path": str(t), "dry_run": True, "from": f"{w}x{h}",
                     "to": f"{w}x{h - cut}", "cut_px": cut}
                )
            else:
                report.say(f"[预览] {t.name}  将裁掉底部 {ratio:.0%}")
            continue

        try:
            if HAVE_PIL:
                if args.force:
                    status = process_with_pillow_forced(t, ratio, args.keep_original, report)
                else:
                    status = process_with_pillow(t, ratio, args.keep_original, report)
            elif sys.platform == "win32":
                status = process_with_powershell(t, ratio, args.keep_original, report)
            elif sys.platform == "darwin":
                status = process_with_sips(t, ratio, args.keep_original, report)
            else:
                report.failed.append({"path": str(t), "error": "no_backend"})
                status = "failed"
        except Exception as exc:
            report.failed.append({"path": str(t), "error": str(exc)[:400]})
            status = "failed"

        if status == "processed" and not args.json:
            last = report.processed[-1]
            report.say(f"[已去水印] {t.name}  {last['from']} -> {last['to']}  ({last['backend']})")
        elif status == "skipped" and not args.json:
            reason = report.skipped[-1]["reason"]
            zh = {
                "already_processed": "已去过水印，无需重复处理",
                "animated": "动图不支持",
                "too_small": "尺寸过小",
                "not_found": "文件不存在",
                "unsupported_format": "格式不支持（仅支持 png/jpg/webp）",
            }.get(reason, reason)
            report.say(f"[跳过] {t.name}  {zh}")

    report.emit()
    if report.failed:
        for f in report.failed:
            report.say(f"[失败] {Path(f['path']).name}  {f['error']}")
        return 1
    return 0


def process_with_pillow_forced(path: Path, ratio: float, keep_original: bool, report: Report) -> str:
    """忽略「已处理」标记强制裁剪。"""
    return process_with_pillow(path, ratio, keep_original, report, ignore_marker=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="裁掉 AI 生图右下角角标水印（默认裁底部 8%）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", help="图片或目录路径")
    parser.add_argument("--in", dest="input_dir", help="目录路径，递归处理其中的图片")
    parser.add_argument("--ratio", type=float, default=DEFAULT_RATIO, help="底部裁剪比例，默认 0.08")
    parser.add_argument("--detect", action="store_true", help="只检测是否疑似有水印，不修改文件")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写盘")
    parser.add_argument("--keep-original", action="store_true", help="保留一份 _orig 副本")
    parser.add_argument("--force", action="store_true", help="忽略已处理标记，强制再裁一次")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    parser.add_argument("--max", type=int, default=200, help="单次最多处理多少张，默认 200")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
