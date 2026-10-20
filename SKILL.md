---
name: ai-image-watermark-remover
display_name: AI生图自动去水印
display_name_en: AI Image Watermark Auto Remover
description: 让 AI 生图后的去水印变成默认动作，用户不必每次开口要求。当任务里出现生成图片、AI 绘图、生图、文生图、图生图、配图、插图、封面、海报、头图、banner、icon、logo 草图、AI 画一张、ImageGen 出图等意图，或图片上出现「AI生成 WORKBUDDY」角标水印需要去除时使用。技能会在生图完成后自动裁掉右下角水印，并可选把该规则写入用户级全局记忆，使之后每个会话都不再需要用户额外指示。也支持对已有图片批量去水印、检测是否疑似有水印、预览裁切效果。
description_zh: AI 生图之后自动去掉右下角「AI生成 WORKBUDDY」水印。装一次，之后每个会话都默认生效，不用再跟 AI 说「把水印去掉」。支持批量、预览、幂等重跑，Windows 无 Python 图像库也能跑。
description_en: Crops the built-in corner watermark off AI-generated images as a default action, without the user having to ask. Use it whenever the task involves generating an image, AI drawing, text-to-image, image-to-image, illustration, cover art, poster, banner, icon or logo draft, or when an existing image still carries the AI-generated WORKBUDDY corner badge. It trims the bottom edge right after generation, can persist the rule into user-level memory so every later session runs it unprompted, and also supports batch cleanup, watermark scoring and dry-run preview.
category: productivity
version: 1.0.0
author: 悟空（WorkBuddy）
license: MIT
agent_created: true
---

# AI 生图自动去水印

## 解决的问题

AI 生图工具会在图片右下角压一行半透明角标（「AI生成 / WORKBUDDY」）。每次出图后都要补一句「把水印去掉」，既费口舌又容易漏。本技能把这件事变成默认动作：**图一生成，就自动裁掉，不需要用户开口**。

水印固定在右下角、紧贴底边，实测占据整图高度 92.9%~98% 区间，随画幅等比缩放。裁掉底部 8% 即可完整去除，宽度不变，视觉损失可以忽略。

实测数据、检测算法的取舍依据、各平台降级方案见 @references/watermark-spec.md。

## 第一步：开启全局规则（每个会话首次用到本技能时执行一次）

> 下文命令里的 `<SKILL_DIR>` 指本文件（SKILL.md）所在目录，即技能根目录。

全局规则写进用户级记忆文件后，**之后每个新会话都会自动带上「生图后去水印」的指令**，不再依赖本技能被触发。这是「无需额外指示」的关键。

```bash
python "<SKILL_DIR>/scripts/global_setup.py" --install
```

- `--install` 本身幂等：内容一致时只回「规则已是最新」，不会重复写入，也不会重复备份
- **技能升级换了安装目录时，必须重跑一次**——它会把规则块里的脚本路径改成新路径，否则全局规则会指向已经不存在的旧文件（`--status` 会把这种状态标成「路径已过期」）
- 安装完成后用一句话告知用户：已开启全局自动去水印，需要关闭时执行 `--uninstall`
- 若用户此前明确表示过不要修改记忆文件，则跳过安装，仅当次任务生效，并说明这一点
- 该脚本只维护一段被注释标记包裹的区块，不会改动记忆文件里的其他内容，并在首次写入前自动备份

## 生图之后的固定动作

拿到图片路径后**立即执行，不要先问用户要不要去水印**：

```bash
python "<SKILL_DIR>/scripts/remove_watermark.py" "<图片路径>" --json
```

- 多张图一次传多个路径；整个目录用 `--in <目录>`
- `ok: true` 时正常交付结果，不需要长篇汇报
- `ok: false` 时读 `failed[].error`，按下面的排障表处理后再交付
- **不要重复调用同一张图**：脚本会在元数据里写幂等标记，重复调用返回 `already_processed`，不会二次裁剪

需要一次处理多张时：

```bash
python "<SKILL_DIR>/scripts/remove_watermark.py" --in "<图片目录>" --json
```

## 参数

| 参数 | 作用 |
| --- | --- |
| `--ratio 0.08` | 底部裁切比例，默认 0.08，安全区间 0.02~0.25 |
| `--detect` | 只检测是否疑似有水印，不修改文件（启发式，仅供参考） |
| `--dry-run` | 只预览会裁掉多少像素，不写盘 |
| `--keep-original` | 保留一份 `_orig` 副本 |
| `--force` | 忽略已处理标记，强制再裁一次 |
| `--json` | 输出结构化结果，便于程序化解析 |
| `--max 200` | 单次最多处理多少张 |

## 边界

- **只处理 AI 生成的图片**。用户自己上传的照片、截图、设计素材、品牌图，一律不要处理——裁掉 8% 会破坏原始内容。
- 动图（GIF）、BMP、TIFF 不支持。需要时先用其他工具转成 png 或 jpg。
- 这是**裁切**，不是像素级修复。水印不在底部、或水印高度超过 8%（画幅极扁时可能出现），要先跟用户确认再调整 `--ratio`。
- 裁切会重编码图片。追求无损原图时用 `--keep-original`。
- 不要把 `--detect` 的结论当成「有没有水印」的判定依据。真正的判据是图片来源：AI 生成的图一定有水印，直接裁即可。

## 环境

优先使用 Pillow。没有 Pillow 时自动降级：

| 平台 | 降级后端 | 说明 |
| --- | --- | --- |
| Windows | System.Drawing（PowerShell） | 零依赖，可直接用；不写幂等标记 |
| macOS | sips | 零依赖；不写幂等标记 |
| Linux | 无 | 需先装 Pillow |

幂等标记只在 Pillow 后端生效。降级后端处理过的图若被再次调用，会被再裁一次——所以降级环境下**同一张图只调用一次**。

## 排障

| 现象 | 处理 |
| --- | --- |
| `no_backend` | `python -m pip install --user pillow` |
| `no_backend`（Linux） | 同上；或改用系统包管理器安装 python3-pil |
| `already_processed` | 正常，说明这张图已经去过水印，直接交付 |
| `unsupported_format` | 先转成 png 或 jpg |
| `animated` | 动图不支持，跳过并说明 |
| PowerShell 报找不到命令 | 确认系统里有 powershell（Windows 7 以上自带） |

## 卸载

- 只关掉全局自动执行：`python "<SKILL_DIR>/scripts/global_setup.py" --uninstall`
- 彻底移除：删掉本技能目录即可，脚本不在系统里留下任何常驻进程或服务
