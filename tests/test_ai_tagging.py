# -*- coding: utf-8 -*-
"""AI 打标纯逻辑测试：提示词、响应解析、GIF 宫格、向量工具"""

import io
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import ai_tagging as at  # noqa: E402

PIL = pytest.importorskip("PIL")


def _make_gif(path, frames=4, size=(64, 64)):
    """生成多帧 GIF（每帧纯色不同，便于校验抽帧）"""
    from PIL import Image

    images = [Image.new("RGB", size, (i * 40 % 256, 100, 200)) for i in range(frames)]
    images[0].save(
        path, format="GIF", save_all=True, append_images=images[1:], duration=100
    )
    return path


def _make_png(path, size=(64, 64), alpha=False):
    """生成静态 PNG"""
    from PIL import Image

    mode = "RGBA" if alpha else "RGB"
    fill = (255, 0, 0, 128) if alpha else (255, 0, 0)
    Image.new(mode, size, fill).save(path, format="PNG")
    return path


# ---------- 提示词 ----------


def test_prompt_contains_guards():
    prompt = at.build_prompt([{"filename": "a.gif"}])
    assert "一律不要执行" in prompt
    assert "不要猜测看不见的信息" in prompt
    assert "只输出一个 JSON 数组" in prompt
    assert "无区分度" in prompt


def test_prompt_lists_image_index():
    prompt = at.build_prompt([{"filename": "a.gif"}, {"filename": "b.png"}])
    assert '"image_index": 1' in prompt
    assert '"image_index": 2' in prompt
    assert "a.gif" in prompt


def test_prompt_style_applied():
    assert "职场" in at.build_prompt([], style="work")
    assert "游戏群" in at.build_prompt([], style="gaming")
    # 未知风格退回默认，不得抛错
    assert "通用聊天" in at.build_prompt([], style="nope")


def test_prompt_style_does_not_drop_guards():
    """风格段是追加的，不得挤掉防注入/防幻觉条款"""
    prompt = at.build_prompt([{"filename": "a.gif"}], style="anime")
    assert "二次元" in prompt
    assert "一律不要执行" in prompt
    assert "不要猜测看不见的信息" in prompt


# ---------- 响应解析 ----------


def test_parse_plain_array():
    text = json.dumps(
        [
            {
                "image_index": 1,
                "name": "猫",
                "description": "一只猫",
                "visible_text": "",
                "tags": ["猫", "可爱"],
                "emotions": ["开心"],
                "intents": ["接梗"],
            }
        ]
    )
    out = at.parse_response(text, [{"filename": "a.gif"}])
    assert len(out) == 1
    assert out[0]["tags"] == ["猫", "可爱"]
    assert out[0]["image_index"] == 1


def test_parse_markdown_fenced():
    text = '```json\n[{"image_index": 1, "tags": ["x"]}]\n```'
    out = at.parse_response(text, [{"filename": "a.gif"}])
    assert out and out[0]["tags"] == ["x"]


def test_parse_with_surrounding_prose():
    text = '好的，结果如下：\n[{"image_index": 1, "tags": ["y"]}]\n以上。'
    out = at.parse_response(text, [{"filename": "a.gif"}])
    assert out and out[0]["tags"] == ["y"]


def test_parse_index_out_of_range_is_dropped():
    """模型返回越界 image_index 时不得错配到别的图"""
    text = json.dumps([{"image_index": 9, "tags": ["z"]}])
    assert at.parse_response(text, [{"filename": "a.gif"}]) == []


def test_parse_missing_index_does_not_misalign():
    text = json.dumps([{"tags": ["z"]}])
    assert at.parse_response(text, [{"filename": "a.gif"}]) == []


def test_parse_respects_limits():
    text = json.dumps(
        [
            {
                "image_index": 1,
                "tags": [f"t{i}" for i in range(30)],
                "emotions": [f"e{i}" for i in range(10)],
                "intents": [f"i{i}" for i in range(10)],
            }
        ]
    )
    out = at.parse_response(text, [{"filename": "a.gif"}])[0]
    assert len(out["tags"]) == 8
    assert len(out["emotions"]) == 4
    assert len(out["intents"]) == 5


def test_parse_ignores_non_string_fields():
    text = json.dumps(
        [{"image_index": 1, "name": {"a": 1}, "tags": ["ok", 1, None, ""]}]
    )
    out = at.parse_response(text, [{"filename": "a.gif"}])[0]
    assert out["name"] == ""
    assert out["tags"] == ["ok"]


def test_parse_garbage_returns_empty():
    assert at.parse_response("完全不是 JSON", [{"filename": "a.gif"}]) == []
    assert at.parse_response("", [{"filename": "a.gif"}]) == []


# ---------- 截断输出的抢救（上游 max_tokens 掐断时不能整批丢弃）----------


def _truncated_payload():
    """模拟真实截断形态：数组未闭合，最后一项残缺"""
    return (
        "[\n"
        '  {"image_index": 1, "name": "点赞喜欢", "description": "Q版女孩比赞",\n'
        '   "visible_text": "", "tags": ["比赞", "点赞"],\n'
        '   "emotions": ["喜爱"], "intents": ["夸奖"]},\n'
        '  {"image_index": 2, "name": "不错呢", "description": "赞赏神情",\n'
        '   "visible_text": "", "tags": ["赞赏"],\n'
        '   "emotions": ["赞许"], "intents": ["认同"]},\n'
        '  {"image_index": 3, "name": '
    )


def test_truncated_output_salvages_complete_entries():
    """输出被截断时，已完整的条目必须救回，而不是整批丢弃

    真实场景：_MAX_TOKENS 过小 + 批次>=3 时被上游掐断（finish_reason=length），
    原实现两条严格路径都失败、返回 None，导致整批记「模型未返回该图结果」。
    """
    items = [{"filename": "a.gif"}, {"filename": "b.gif"}, {"filename": "c.gif"}]
    out = at.parse_response(_truncated_payload(), items)
    assert len(out) == 2, "应救回前两条完整对象"
    assert [o["image_index"] for o in out] == [1, 2]
    assert out[0]["name"] == "点赞喜欢"
    assert out[1]["tags"] == ["赞赏"]


def test_truncated_output_missing_entry_not_misaligned():
    """残缺的第 3 条不能被错位归到别的 index 上"""
    items = [{"filename": "a.gif"}, {"filename": "b.gif"}, {"filename": "c.gif"}]
    out = at.parse_response(_truncated_payload(), items)
    assert 3 not in [o["image_index"] for o in out]


def test_salvage_handles_escaped_quotes_and_nesting():
    """抢救扫描必须正确跳过字符串内/转义引号与嵌套对象"""
    text = (
        '[{"image_index":1,"name":"含\\"引号\\"的值","tags":["a"]},'
        '{"image_index":2,"name":"第二","nested":{"k":[1,2]},"tags":["b"]},'
        '{"image_index":3,"name":"残'
    )
    out = at.parse_response(text, [{}, {}, {}])
    assert len(out) == 2
    assert out[0]["name"] == '含"引号"的值'
    assert out[1]["name"] == "第二"


def test_salvage_does_not_break_valid_input():
    """正常闭合的输入仍走严格解析，结果不受兜底路径影响"""
    text = json.dumps(
        [
            {"image_index": 1, "name": "一", "tags": ["x"]},
            {"image_index": 2, "name": "二", "tags": ["y"]},
        ],
        ensure_ascii=False,
    )
    out = at.parse_response(text, [{}, {}])
    assert [o["name"] for o in out] == ["一", "二"]


def test_max_tokens_large_enough_for_default_batch():
    """输出上限必须容纳默认批次的 JSON（1200 会在批次>=3 时被截断）"""
    # 单张约 230~270 字符，默认批次 4 张 => 需 >1000 token 的输出空间
    assert at._MAX_TOKENS >= 2048, (
        "_MAX_TOKENS=%s 过小，批次>=3 时会被上游截断" % at._MAX_TOKENS
    )


#
# ---------- 图片编码与 GIF 宫格 ----------


def test_png_encoded_as_data_url(tmp_path):
    p = _make_png(tmp_path / "a.png")
    url = at.encode_image_for_vision(str(p))
    assert url.startswith("data:image/jpeg;base64,")


def test_png_with_alpha_uses_png(tmp_path):
    p = _make_png(tmp_path / "a.png", alpha=True)
    assert at.encode_image_for_vision(str(p)).startswith("data:image/png;base64,")


def test_gif_becomes_single_still_grid(tmp_path):
    """多帧 GIF 应被抽帧拼成 2x2 宫格，且输出为 PNG（保留透明通道）"""
    from PIL import Image

    p = _make_gif(tmp_path / "a.gif", frames=4)
    url = at.encode_image_for_vision(str(p))
    assert url is not None
    payload = url.split(",", 1)[1]
    import base64

    img = Image.open(io.BytesIO(base64.b64decode(payload)))
    assert img.mode in ("RGBA", "LA", "P")
    assert img.width <= 1024 and img.height <= 1024


def test_gif_grid_row_count_matches_frames(tmp_path):
    """帧数少于 4 时行数应随样本数减少（不得固定 2 行）"""
    from PIL import Image

    p = _make_gif(tmp_path / "b.gif", frames=2)
    url = at.encode_image_for_vision(str(p), max_edge=256)
    import base64

    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    # 2 帧 + 2 列 = 1 行；cell_edge = 128 → 宽度 256，高度 128
    assert img.width == 256
    assert img.height == 128


def test_max_edge_respected(tmp_path):
    p = _make_gif(tmp_path / "c.gif", frames=4)
    url = at.encode_image_for_vision(str(p), max_edge=200)
    import base64

    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(img.width, img.height) <= 200


def test_broken_gif_returns_none(tmp_path):
    """损坏的 .gif 必须返回 None —— 未解码 GIF 会让整批分析 500 全灭"""
    p = tmp_path / "broken.gif"
    p.write_bytes(b"GIF89a" + b"\x00" * 32)
    assert at.encode_image_for_vision(str(p)) is None


def test_broken_png_falls_back_to_raw_bytes(tmp_path):
    """非 GIF 解码失败不得跳过，应退回原始字节（否则白丢标签）"""
    p = tmp_path / "broken.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    url = at.encode_image_for_vision(str(p))
    assert url is not None
    assert url.startswith("data:image/png;base64,")


def test_missing_file_returns_none(tmp_path):
    assert at.encode_image_for_vision(str(tmp_path / "nope.gif")) is None


def test_single_frame_gif_not_broken(tmp_path):
    """单帧 GIF 走静态分支（旧实现此处会除零）"""
    from PIL import Image

    p = tmp_path / "one.gif"
    Image.new("RGB", (32, 32), (1, 2, 3)).save(p, format="GIF")
    url = at.encode_image_for_vision(str(p))
    assert url is not None


# ---------- 端点归一化 ----------


def test_normalize_endpoint_variants():
    assert at.normalize_endpoint("https://x.com/v1/") == "https://x.com/v1"
    assert at.normalize_endpoint("https://x.com") == "https://x.com/v1"
    assert at.normalize_endpoint("x.com") == "https://x.com/v1"
    assert at.normalize_endpoint("") == ""


# ---------- 向量工具 ----------


def test_l2_normalize_unit_norm():
    v = at.l2_normalize([3.0, 4.0])
    assert abs(sum(x * x for x in v) - 1.0) < 1e-9


def test_l2_normalize_zero_vector():
    assert at.l2_normalize([0.0, 0.0]) == [0.0, 0.0]


def test_pack_unpack_roundtrip():
    vec = [0.5, -0.25, 1.0, 0.125]
    assert at.unpack_vector(at.pack_vector(vec), 4) == pytest.approx(vec)


def test_pack_size_is_dim_times_four():
    assert len(at.pack_vector([0.0] * 768)) == 768 * 4


def test_unpack_rejects_wrong_length():
    assert at.unpack_vector(b"\x00" * 8, 4) == []
    assert at.unpack_vector(None, 4) == []


def test_dot_matches_pure_python():
    """numpy 与纯 Python 两条路径结果必须一致"""
    a = [0.1, 0.2, 0.3, 0.4]
    b = [0.5, 0.6, 0.7, 0.8]
    expected = sum(x * y for x, y in zip(a, b))
    assert at.dot(a, b) == pytest.approx(expected, abs=1e-6)


def test_cosine_topk_orders_by_similarity():
    rows = [(1, [1.0, 0.0]), (2, [0.0, 1.0]), (3, [0.9, 0.1])]
    q = at.l2_normalize([1.0, 0.0])
    out = at.cosine_topk(q, rows, 2)
    assert [mid for mid, _ in out] == [1, 3]


def test_cosine_topk_empty_inputs():
    assert at.cosine_topk([], [(1, [1.0])], 3) == []
    assert at.cosine_topk([1.0], [], 3) == []


def test_cosine_topk_skips_empty_vectors():
    out = at.cosine_topk([1.0, 0.0], [(1, []), (2, [1.0, 0.0])], 5)
    assert [mid for mid, _ in out] == [2]


def test_build_embed_text_stable_and_complete():
    row = {
        "ai_name": "猫",
        "ai_description": "一只猫",
        "ai_visible_text": "无语",
        "ai_emotions": ["开心"],
        "ai_intents": ["接梗"],
        "tags": ["可爱"],
    }
    t1 = at.build_embed_text(row)
    t2 = at.build_embed_text(row)
    assert t1 == t2
    for part in ("猫", "一只猫", "无语", "开心", "接梗", "可爱"):
        assert part in t1


def test_build_embed_text_tolerates_missing_fields():
    assert at.build_embed_text({}) == ""


def test_text_hash_changes_with_content():
    assert at.text_hash("a") != at.text_hash("b")
    assert at.text_hash("a") == at.text_hash("a")


# ---------- 错误路径 ----------


def test_call_vision_requires_endpoint_and_model():
    with pytest.raises(ValueError):
        at.call_vision("", "k", "m", "p", ["data:,"])
    with pytest.raises(ValueError):
        at.call_vision("https://x.com", "k", "", "p", ["data:,"])


def test_embed_texts_requires_endpoint():
    with pytest.raises(ValueError):
        at.embed_texts("", "k", "m", ["x"])


# ---------- M5 语义检索 ----------


def _mk_db_with_vectors(tmp_path, monkeypatch):
    """造带向量的库，并把全局单例指向它（供 webui 检索函数使用）"""
    from src import database as database_mod

    db = database_mod.MemeDB(tmp_path / "s.db")
    vectors = {
        1: [1.0, 0.0, 0.0],  # 开心
        2: [0.0, 1.0, 0.0],  # 嘲讽
        3: [0.0, 0.0, 1.0],  # 疑惑
    }
    with db._lock:
        conn = db._get_conn()
        for mid in vectors:
            conn.execute(
                "INSERT INTO memes (id, filename, original_name, file_hash, width, "
                "height, mime_type, ai_status) VALUES (?,?,?,?,?,?,?,'done')",
                (
                    mid,
                    "f%d.png" % mid,
                    "名字%d" % mid,
                    "h%d" % mid,
                    10,
                    10,
                    "image/png",
                ),
            )
        conn.commit()
    for mid, vec in vectors.items():
        norm = at.l2_normalize(vec)
        db.apply_embedding(mid, at.pack_vector(norm), "m", len(norm), "hash%d" % mid)
    monkeypatch.setattr(database_mod, "_db", db)
    return db


def test_cosine_topk_orders_by_similarity():
    rows = [(1, [1.0, 0.0]), (2, [0.0, 1.0]), (3, [0.7, 0.7])]
    got = at.cosine_topk([1.0, 0.0], rows, 2)
    assert [mid for mid, _ in got] == [1, 3]


def test_get_by_ids_preserves_nothing_but_returns_rows(tmp_path):
    """get_by_ids 不保证顺序（顺序由调用方按分数重排），但必须返回全部命中行"""
    db, mid = _make_db(tmp_path)
    rows = db.get_by_ids([mid])
    assert len(rows) == 1 and rows[0]["id"] == mid
    assert db.get_by_ids([]) == []


def test_filter_ids_by_scope_tag_intersection(tmp_path):
    """多标签必须取交集，与 search()/count() 语义一致"""
    db, mid = _make_db(tmp_path)
    db.set_meme_tags(mid, ["a", "b"])
    assert db.filter_ids_by_scope([mid], tags=["a"]) == [mid]
    assert db.filter_ids_by_scope([mid], tags=["a", "b"]) == [mid]
    assert db.filter_ids_by_scope([mid], tags=["a", "c"]) == []


def test_filter_ids_by_scope_favorite_and_collection(tmp_path):
    db, mid = _make_db(tmp_path)
    assert db.filter_ids_by_scope([mid], favorite_only=True) == []
    db.toggle_favorite(mid)
    assert db.filter_ids_by_scope([mid], favorite_only=True) == [mid]


def test_query_embedding_cache_hits(tmp_path, monkeypatch):
    """同一查询词必须复用向量，否则每次搜索都要调一次嵌入接口"""
    from src import webui

    calls = []

    def fake_embed(endpoint, api_key, model, texts, timeout=None):
        calls.append(texts[0])
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(at, "embed_texts", fake_embed)
    webui._QUERY_VEC_CACHE.clear()
    v1 = webui._query_embedding("e", "k", "m", "缓存词A")
    v2 = webui._query_embedding("e", "k", "m", "缓存词A")
    assert v1 == v2
    assert len(calls) == 1, "第二次应命中缓存，实际调用了 %d 次" % len(calls)
    webui._query_embedding("e", "k", "m", "缓存词B")
    assert len(calls) == 2, "不同查询词应各调一次"
    webui._QUERY_VEC_CACHE.clear()


def test_query_embedding_returns_empty_on_failure(monkeypatch):
    """嵌入调用失败必须返回空列表（由调用方回退关键字），不能抛给上层"""
    from src import webui

    def boom(*a, **kw):
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(at, "embed_texts", boom)
    webui._QUERY_VEC_CACHE.clear()
    assert webui._query_embedding("e", "k", "m", "会失败") == []


def test_semantic_search_disabled_when_ai_off(tmp_path, monkeypatch):
    """关总开关 / 关嵌入 / 未配模型 / 空关键词 时必须返回 None（回退关键字）"""
    from src import webui

    db = _mk_db_with_vectors(tmp_path, monkeypatch)
    api = webui.JsApi.__new__(webui.JsApi)

    class Cfg:
        def __init__(self, data):
            self._d = dict(data)

        def get(self, k, d=None):
            return self._d.get(k, d)

    base = {
        "ai_enabled": True,
        "ai_embed_enabled": True,
        "ai_embed_endpoint": "https://x/v1",
        "ai_embed_model": "m",
    }
    for override, label in (
        ({"ai_enabled": False}, "关总开关"),
        ({"ai_embed_enabled": False}, "关嵌入"),
        ({"ai_embed_model": ""}, "未配模型"),
        ({"ai_embed_endpoint": ""}, "未配端点"),
    ):
        data = dict(base)
        data.update(override)
        api._cfg = Cfg(data)
        api._db = db
        got = api._semantic_search_ids("任意词", None, None, False, False)
        assert got is None, "%s 时应返回 None（回退），实际 %r" % (label, got)
    # 空关键词始终回退
    api._cfg = Cfg(base)
    assert api._semantic_search_ids("", None, None, False, False) is None


def test_semantic_search_falls_back_when_no_vectors(tmp_path, monkeypatch):
    """库中无向量时必须回退关键字，否则搜索会变成空结果"""
    from src import webui

    db, _mid = _make_db(tmp_path)  # 无向量
    api = webui.JsApi.__new__(webui.JsApi)
    api._db = db

    class Cfg:
        def get(self, k, d=None):
            return {
                "ai_enabled": True,
                "ai_embed_enabled": True,
                "ai_embed_endpoint": "https://x/v1",
                "ai_embed_model": "m",
            }.get(k, d)

    api._cfg = Cfg()
    monkeypatch.setattr(webui, "_query_embedding", lambda *a, **kw: [0.1, 0.2, 0.3])
    assert api._semantic_search_ids("词", None, None, False, False) is None


def test_semantic_search_ranks_by_similarity(tmp_path, monkeypatch):
    """语义命中的 id 必须按相似度降序返回"""
    from src import webui

    db = _mk_db_with_vectors(tmp_path, monkeypatch)
    api = webui.JsApi.__new__(webui.JsApi)
    api._db = db

    class Cfg:
        def get(self, k, d=None):
            return {
                "ai_enabled": True,
                "ai_embed_enabled": True,
                "ai_embed_endpoint": "https://x/v1",
                "ai_embed_model": "m",
                "ai_embed_top_k": 30,
            }.get(k, d)

    api._cfg = Cfg()
    # 查询向量最接近 id=2 的 [0,1,0]
    monkeypatch.setattr(webui, "_query_embedding", lambda *a, **kw: [0.0, 1.0, 0.0])
    got = api._semantic_search_ids("嘲讽", None, None, False, False)
    assert got is not None and got[0] == 2, "首位应为最相似者，实际 %r" % (got,)
    assert set(got) == {1, 2, 3}


def test_search_memes_uses_semantic_branch(tmp_path, monkeypatch):
    """分流本身：语义可用时 search_memes 必须走语义分支（而非关键字）"""
    from src import webui

    db = _mk_db_with_vectors(tmp_path, monkeypatch)
    api = webui.JsApi.__new__(webui.JsApi)
    api._db = db
    api._webui = None

    class Cfg:
        def get(self, k, d=None):
            return {
                "ai_enabled": True,
                "ai_embed_enabled": True,
                "ai_embed_endpoint": "https://x/v1",
                "ai_embed_model": "m",
                "ai_embed_top_k": 30,
                "auto_play_gif": True,
                "hover_to_play": False,
            }.get(k, d)

    api._cfg = Cfg()
    monkeypatch.setattr(webui, "_query_embedding", lambda *a, **kw: [0.0, 1.0, 0.0])
    # 关键词在库里任何字段都不存在，只有语义分支才可能返回结果
    rows = api.search_memes("完全不存在的字面词", None, None, 0, 10)
    assert len(rows) == 3, "语义分支应返回全部 3 条，实际 %d" % len(rows)
    assert rows[0]["id"] == 2, "应按相似度排序，首位为 id=2，实际 %r" % rows[0]["id"]
    # 计数必须跟随语义，否则分页对不上
    assert api.count_memes("完全不存在的字面词", None, None) == 3


def test_search_memes_falls_back_to_keyword(tmp_path, monkeypatch):
    """语义不可用时 search_memes 必须走原关键字路径（未开 AI 的用户零变化）"""
    from src import webui

    db, mid = _make_db(tmp_path)  # 无向量
    api = webui.JsApi.__new__(webui.JsApi)
    api._db = db
    api._webui = None

    class Cfg:
        def get(self, k, d=None):
            return {
                "ai_enabled": False,
                "auto_play_gif": True,
                "hover_to_play": False,
            }.get(k, d)

    api._cfg = Cfg()
    with db._lock:
        conn = db._get_conn()
        conn.execute("UPDATE memes SET original_name='猫猫表情' WHERE id=?", (mid,))
        conn.commit()
    rows = api.search_memes("猫猫", None, None, 0, 10)
    assert len(rows) == 1, "关键字路径应命中 1 条，实际 %d" % len(rows)
    assert api.count_memes("猫猫", None, None) == 1


def test_post_json_reports_http_error(monkeypatch):
    """HTTP 错误须带上状态码与响应体，便于设置页显示可读原因"""
    import urllib.error

    def boom(*a, **kw):
        raise urllib.error.HTTPError(
            "u", 401, "Unauthorized", {}, io.BytesIO(b"bad key")
        )

    monkeypatch.setattr(at.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as e:
        at._post_json("https://x.com/v1/chat/completions", "k", {})
    assert "401" in str(e.value)
    assert "bad key" in str(e.value)


def test_post_json_reports_bad_json(monkeypatch):
    class Resp:
        def read(self):
            return b"not json"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(at.urllib.request, "urlopen", lambda *a, **kw: Resp())
    with pytest.raises(RuntimeError) as e:
        at._post_json("https://x.com/v1/chat/completions", "k", {})
    assert "JSON" in str(e.value)


def test_rerank_parses_and_sorts(monkeypatch):
    payload = {
        "results": [
            {"index": 1, "relevance_score": 0.2},
            {"index": 0, "relevance_score": 0.9},
        ]
    }
    monkeypatch.setattr(at, "_post_json", lambda *a, **kw: payload)
    out = at.rerank("https://x.com", "k", "m", "q", ["a", "b"])
    assert out == [(0, 0.9), (1, 0.2)]


def test_rerank_accepts_score_alias(monkeypatch):
    payload = {"data": [{"index": 0, "score": 0.5}]}
    monkeypatch.setattr(at, "_post_json", lambda *a, **kw: payload)
    assert at.rerank("https://x.com", "k", "m", "q", ["a"]) == [(0, 0.5)]


def test_embed_texts_sorts_by_index(monkeypatch):
    payload = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0]},
            {"index": 0, "embedding": [1.0, 0.0]},
        ]
    }
    monkeypatch.setattr(at, "_post_json", lambda *a, **kw: payload)
    out = at.embed_texts("https://x.com", "k", "m", ["a", "b"])
    assert out == [[1.0, 0.0], [0.0, 1.0]]


# ---------- 数据库层：标签只追加 / 待嵌入幂等 ----------


def _make_db(tmp_path):
    from src.database import MemeDB

    db = MemeDB(tmp_path / "memes.db")
    mid = db.add_meme("a.gif", "hash-a", "a.gif", 32, 32, 100, "image/gif")
    return db, mid


def test_apply_ai_result_appends_never_overwrites(tmp_path):
    """人工标签在先时，应用 AI 结果必须只追加 —— 覆盖是不可逆的数据损失"""
    db, mid = _make_db(tmp_path)
    db.set_meme_tags(mid, ["人工标签"])
    db.apply_ai_result(
        mid,
        {
            "tags": ["AI标签"],
            "name": "n",
            "description": "d",
            "visible_text": "",
            "emotions": ["e"],
            "intents": ["i"],
        },
    )
    tags = set(db.get_meme_tags(mid))
    assert "人工标签" in tags
    assert "AI标签" in tags


def test_apply_ai_result_missing_meme_raises_clear_error(tmp_path):
    """对不存在的表情应用结果须抛可读错误，而非 FOREIGN KEY constraint failed"""
    db, _ = _make_db(tmp_path)
    with pytest.raises(ValueError) as e:
        db.apply_ai_result(999999, {"tags": ["x"]})
    assert "表情不存在" in str(e.value)


def test_apply_ai_result_writes_fields_and_clears_suggestion(tmp_path):
    db, mid = _make_db(tmp_path)
    db.store_ai_suggestion(mid, {"tags": ["t"], "name": "名字"})
    assert db.count_ai_suggestions() == 1
    db.apply_ai_result(mid, {"tags": ["t"], "name": "名字"})
    assert db.count_ai_suggestions() == 0
    row = db.get_by_id(mid)
    assert row["ai_name"] == "名字"
    assert row["ai_status"] == "done"


def test_list_embed_pending_exposes_embedding_column(tmp_path):
    """必须带出 embedding 列，否则幂等判据恒为 False、每次都重复调用嵌入接口"""
    db, mid = _make_db(tmp_path)
    db.apply_ai_result(mid, {"tags": ["t"]})
    rows = db.list_embed_pending()
    assert len(rows) == 1
    assert "embedding" in rows[0]
    assert rows[0]["embedding"] is None


def test_list_embed_pending_reflects_existing_vector(tmp_path):
    """已有向量时该列必须非 None（幂等判据依赖它）"""
    db, mid = _make_db(tmp_path)
    db.apply_ai_result(mid, {"tags": ["t"]})
    db.apply_embedding(mid, at.pack_vector([1.0, 0.0]), "m", 2, "h")
    rows = db.list_embed_pending()
    assert rows[0]["embedding"] is not None


def test_clear_ai_error_resets_to_pending(tmp_path):
    db, mid = _make_db(tmp_path)
    db.mark_ai_failed([mid], "boom")
    assert db.list_ai_failed_ids() == [mid]
    db.clear_ai_error([mid])
    assert db.list_ai_failed_ids() == []
    assert [r["id"] for r in db.list_ai_pending()] == [mid]


def test_discard_ai_suggestions_does_not_touch_tags(tmp_path):
    db, mid = _make_db(tmp_path)
    db.store_ai_suggestion(mid, {"tags": ["被丢弃"]})
    db.discard_ai_suggestions()
    assert db.count_ai_suggestions() == 0
    assert db.get_all_tags() == []


def test_list_ai_pending_includes_stuck_running(tmp_path):
    """中断遗留的 running 行必须能被重新拾起，否则成为永久僵尸数据

    真实场景：任务开始整批置 running，进程中途退出后未处理完的行停在
    running；若 list_ai_pending 不取 running，这些行再也不会被处理。
    """
    db, mid = _make_db(tmp_path)
    db.mark_ai_running([mid])
    assert db.list_ai_pending(), "running 行应被重新拾起"
    assert [r["id"] for r in db.list_ai_pending()] == [mid]


def test_list_ai_pending_excludes_done(tmp_path):
    """已完成的不能被重复处理（避免重复烧额度）"""
    db, mid = _make_db(tmp_path)
    db.apply_ai_result(mid, {"tags": ["x"]})
    assert db.list_ai_pending() == []


def test_delete_meme_cascades_suggestion(tmp_path):
    db, mid = _make_db(tmp_path)
    db.store_ai_suggestion(mid, {"tags": ["x"]})
    db.delete_meme(mid)
    assert db.count_ai_suggestions() == 0


def test_old_db_migrates_ai_columns_and_table(tmp_path):
    """旧库（无 ai_ 列）打开时应自动补齐列、建表与索引"""
    import sqlite3

    p = tmp_path / "old.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
        CREATE TABLE memes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            file_hash TEXT NOT NULL DEFAULT '',
            original_name TEXT NOT NULL DEFAULT '',
            width INTEGER DEFAULT 0, height INTEGER DEFAULT 0,
            file_size INTEGER DEFAULT 0, mime_type TEXT DEFAULT 'image/png',
            sort_order INTEGER DEFAULT 0, stego_of_hash TEXT DEFAULT NULL,
            from_stego INTEGER DEFAULT 0, perceptual_hash TEXT DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT,
                           name TEXT NOT NULL UNIQUE COLLATE NOCASE);
        CREATE TABLE meme_tags (
            meme_id INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (meme_id, tag_id));
        CREATE TABLE collections (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE,
            parent_id INTEGER DEFAULT NULL REFERENCES collections(id) ON DELETE CASCADE,
            sort_order INTEGER DEFAULT 0);
        CREATE TABLE meme_collections (
            meme_id INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            sort_order INTEGER DEFAULT 0, PRIMARY KEY (meme_id, collection_id));
        CREATE TABLE favorites (
            meme_id INTEGER PRIMARY KEY REFERENCES memes(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
        CREATE TABLE recent_uses (
            meme_id INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
            used_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (meme_id));
        """)
    c.execute("INSERT INTO memes (filename) VALUES ('old.gif')")
    c.commit()
    c.close()

    from src.database import MemeDB

    db = MemeDB(p)
    cols = {r[1] for r in db._get_conn().execute("PRAGMA table_info(memes)").fetchall()}
    for col in (
        "ai_name",
        "ai_description",
        "ai_visible_text",
        "ai_emotions",
        "ai_intents",
        "ai_status",
        "ai_error",
        "ai_provider",
        "ai_analyzed_at",
        "embedding",
        "embedding_model",
        "embedding_dim",
        "embedding_text_hash",
        "embedded_at",
    ):
        assert col in cols, col
    tables = {
        r[0]
        for r in db._get_conn()
        .execute("SELECT name FROM sqlite_master WHERE type='table'")
        .fetchall()
    }
    assert "ai_suggestions" in tables
    # 迁移必须幂等
    db._migrate(db._get_conn())
    assert db.list_ai_pending()[0]["filename"] == "old.gif"


# ---------- 前端接线契约（M4） ----------


def test_get_init_data_and_search_expose_ai_status(tmp_path, monkeypatch):
    """两处序列化都要带出 ai_status，否则首屏有角标、翻页后消失"""
    import inspect

    from src import webui

    # 源码级守卫：两个序列化点都必须输出该字段
    assert '"ai_status"' in inspect.getsource(webui.JsApi.search_memes)
    assert '"ai_status"' in inspect.getsource(webui.JsApi.get_init_data)


def test_db_search_carries_ai_status(tmp_path):
    """db.search 用 SELECT m.*，ai_status 必须随之带出"""
    db, mid = _make_db(tmp_path)
    assert db.search("", None, None, False, False, 0, 10)[0]["ai_status"] is None
    db.mark_ai_running([mid])
    assert db.search("", None, None, False, False, 0, 10)[0]["ai_status"] == "running"


def test_get_ai_review_items_includes_existing_tags(tmp_path, monkeypatch):
    """审核弹窗要只读展示已有标签，故建议项必须附带 existing_tags"""
    from src import database as database_mod
    from src import webui

    db, mid = _make_db(tmp_path)
    monkeypatch.setattr(database_mod, "_db", db)
    db.set_meme_tags(mid, ["人工标签"])
    db.store_ai_suggestion(mid, {"tags": ["猫", "可爱"], "name": "猫猫"})
    res = webui.get_ai_review_items()
    assert res["ok"] is True
    assert len(res["items"]) == 1
    item = res["items"][0]
    assert item["existing_tags"] == ["人工标签"]
    assert item["tags"] == ["猫", "可爱"]
    assert item["filename"]


def test_get_ai_review_items_survives_missing_tags(tmp_path, monkeypatch):
    """已有标签读取异常时必须退空列表，不能整批失败"""
    from src import database as database_mod
    from src import webui

    db, mid = _make_db(tmp_path)
    monkeypatch.setattr(database_mod, "_db", db)
    db.store_ai_suggestion(mid, {"tags": ["x"]})

    def boom(_):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_meme_tags", boom)
    res = webui.get_ai_review_items()
    assert res["items"][0]["existing_tags"] == []


def test_get_ai_summary_shape(tmp_path, monkeypatch):
    """主窗口角标依赖 pending_review 与 pending_ids"""
    from src import database as database_mod
    from src import webui

    db, mid = _make_db(tmp_path)
    monkeypatch.setattr(database_mod, "_db", db)
    db.store_ai_suggestion(mid, {"tags": ["x"]})
    sm = webui.get_ai_summary()
    assert sm["ok"] is True
    assert sm["pending_review"] == 1
    assert sm["pending_ids"] == [mid]
    assert isinstance(sm["stats"], dict)


def test_get_ai_detail_shape(tmp_path, monkeypatch):
    """AI 解读弹窗的数据契约"""
    from src import database as database_mod
    from src import webui

    db, mid = _make_db(tmp_path)
    monkeypatch.setattr(database_mod, "_db", db)
    db.apply_ai_result(
        mid,
        {
            "tags": ["标签"],
            "name": "名字",
            "description": "描述",
            "visible_text": "文字",
            "emotions": ["开心"],
            "intents": ["问候"],
            "provider": "m",
        },
    )
    res = webui.get_ai_detail(mid)
    assert res["ok"] is True
    d = res["detail"]
    assert d["name"] == "名字"
    assert d["emotions"] == ["开心"]
    assert d["intents"] == ["问候"]
    assert d["tags"] == ["标签"]
    assert d["status"] == "done"
    # 不存在的表情要给可读错误而非抛异常
    assert webui.get_ai_detail(999999)["ok"] is False


def test_jsapi_has_ai_forwarders():
    """主窗口绑的是 JsApi，缺转发方法则右键入口全部无效"""
    from src import webui

    for name in (
        "ai_summary",
        "ai_get_settings",
        "ai_tag_start",
        "ai_tag_get_progress",
        "ai_tag_cancel",
        "ai_tag_retry_failed",
        "ai_review_items",
        "ai_detail",
        "ai_tag_apply",
        "ai_tag_apply_all",
        "ai_tag_discard",
    ):
        assert hasattr(webui.JsApi, name), name
