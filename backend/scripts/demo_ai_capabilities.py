"""★ 三种新 AI 能力的**可观测演示**（产出真实输出，供验收贴证据，不写库、不改配置）。

跑法：
    backend/.venv/Scripts/python.exe backend/scripts/demo_ai_capabilities.py

做三件事：
    1. 造 3 张图 + 3 条不同逐图提示词，打印 file_bridge 写出的 prompt.md 关键片段；
    2. 打印 `AiInputPrompt.image_prompt(i)` 在各 index 上的取值与回落；
    3. 打印 mock 的标题候选（≥3 条互不相同 + 选择依据）与短视频脚本文案。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.adapters.ai.base import AiImagePrompt, AiInputPrompt, AiTaskContext  # noqa: E402
from app.adapters.ai.factory import AiClientFactory  # noqa: E402
from app.adapters.ai.file_bridge import WorkBuddyFileBridgeClient  # noqa: E402
from app.adapters.ai.mock_client import MockAiClient  # noqa: E402
from app.models.enums import AiTaskType  # noqa: E402

PATHS = ["data/assets/raw/a.png", "data/assets/raw/b.png", "data/assets/raw/c.png"]
PROMPTS = ["主图换成暖色背景、留白多一点", "第二张加生活化场景，有人物", "第三张微距突出面料纹理"]


def _rule(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # ---------- 1. 逐图提示词 → prompt.md ----------
        _rule("验证 2｜逐图提示词是否真的进了 file_bridge 的 prompt.md")
        bridge = WorkBuddyFileBridgeClient(
            queue_dir=tmp_path / "queue",
            output_dir=tmp_path / "output",
            poll_timeout_sec=2.0,
            poll_interval_sec=0.1,
        )
        ctx = AiTaskContext(
            task_id="demo-001",
            target_platform="taobao",
            source_product_id=1,
            original_title="纯棉四件套 1.8m 床",
            selling_points=["亲肤透气", "不起球"],
            source_image_paths=PATHS,
            image_prompts=[AiImagePrompt(index=i, prompt=p) for i, p in enumerate(PROMPTS)],
            global_prompt="整体偏日式极简",
            task_type=AiTaskType.IMAGE_REDRAW.value,
        )
        written = bridge.write_task(ctx)
        text = Path(written["prompt_md"]).read_text(encoding="utf-8")
        start = text.find("## 原始图片")
        end = text.find("## 卖点参考")
        print(f"prompt.md 路径：{written['prompt_md']}")
        print("-" * 78)
        print(text[start:end].rstrip())
        print("-" * 78)
        for path, prompt in zip(PATHS, PROMPTS):
            assert f"`{path}`" in text and f"提示词：{prompt}" in text, f"逐图提示词没对上：{path}"
            assert text.find(f"`{path}`") < text.find(f"提示词：{prompt}")
        print("结论：3 张原图与 3 条提示词在 prompt.md 中**一一对应**（路径位置 < 提示词位置）")

        # ---------- 2. 取值与回落 ----------
        _rule("验证 2｜AiInputPrompt / AiTaskContext 的取值口径")
        prompt = AiInputPrompt.from_dict(ctx.to_dict() | {"images": ctx.to_dict()["image_prompts"]})
        for index in range(4):
            print(
                f"  index={index}: image_prompt()={prompt.image_prompt(index)!r:<20} "
                f"effective_prompt()={prompt.effective_prompt(index)!r:<20} "
                f"ctx.image_prompt()={ctx.image_prompt(index)!r}"
            )
        assert prompt.image_prompt(1) == PROMPTS[1]
        assert ctx.image_prompt(9) == "整体偏日式极简", "缺项必须回落到全局提示词"
        print("结论：有逐图取逐图；index 没有对应项时回落到全局提示词")

        # ---------- 3. 三个客户端 × 三种能力 ----------
        _rule("验证 1｜三个客户端都能实例化，三种能力都能调通")
        for name in ("mock", "file_bridge", "http"):
            client = AiClientFactory.instantiate(name)
            print(f"  instantiate({name!r}) -> {type(client).__name__}")

        # ---------- 4. 标题候选 ----------
        _rule("验证 3｜标题候选（≥3 条互不相同，每条带选择依据）")
        mock = MockAiClient()
        titles = await mock.suggest_titles(
            AiTaskContext(
                task_id="demo-002",
                target_platform="douyin",
                original_title="纯棉四件套 1.8m 床",
                selling_points=["亲肤透气", "不起球", "支持机洗"],
                title_prompt="偏抖音热词风",
                task_type=AiTaskType.TITLE_SUGGEST.value,
            )
        )
        for position, candidate in enumerate(titles.candidates, start=1):
            print(f"  [{position}] {candidate.title}")
            print(f"      风格={candidate.style}｜平台={candidate.platform_fit or '通用'}｜字数={candidate.char_count}｜分={candidate.score}")
            print(f"      依据={candidate.reason}")
        assert len({c.title for c in titles.candidates}) == len(titles.candidates) >= 3
        best = titles.preferred()
        print(f"结论：{len(titles.candidates)} 条候选互不重复，默认选中「{best.title}」（平台 {best.platform_fit}）")

        # ---------- 5. 视频脚本 ----------
        _rule("验证 3｜短视频拍摄脚本（只出文案）")
        script = await mock.suggest_video_script(
            AiTaskContext(
                task_id="demo-003",
                target_platform="douyin",
                original_title="纯棉四件套 1.8m 床",
                selling_points=["亲肤透气", "不起球"],
                video_script_prompt="竖屏、快节奏、强调性价比",
                task_type=AiTaskType.VIDEO_SCRIPT.value,
            )
        )
        print(script.to_text())

        # ---------- 6. 脏数据 ----------
        _rule("验证 4｜空 / 脏 input_prompt_json 不得抛异常")
        for dirty in (None, {}, "乱码", {"images": [None, {"index": "x"}]}, [1, 2]):
            parsed = AiInputPrompt.from_dict(dirty)
            print(f"  from_dict({dirty!r}) -> global_prompt={parsed.global_prompt!r}, images={len(parsed.images)}")


if __name__ == "__main__":
    asyncio.run(main())
    print("\n全部演示断言通过。")
