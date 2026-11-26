"""Deterministic lexical film context, kept separate from prediction targets.

Feature hashing preserves recurring spellings; it is not a semantic text model.
The synopsis is an explicitly supplied whole-film prior. Timed events are only
visible at their annotated time, never broadcast backwards into earlier frames.
"""
from collections import defaultdict
import hashlib
import re
import unicodedata

import numpy as np


VERSION = 1
HASH_SIZE = 32
NAMES = ([f"cast_text_{i}" for i in range(HASH_SIZE)] +
         [f"synopsis_text_{i}" for i in range(HASH_SIZE)] +
         [f"event_text_{i}" for i in range(HASH_SIZE)] +
         ["cast_context_available", "synopsis_context_available", "event_context_available"])
NOTE = ("已确认演员/角色和整片剧情是额外已知资料；文本哈希只提供词面条件，不理解故事语义。"
        "有剧情资料的验证属于提供剧情先验的条件预测，不等同无未来剧情先验的预测。"
        "时间段剧情仅进入对应历史时刻，资料维度不作为预测目标或计入误差。")


def confirmed(profile):
    return profile if isinstance(profile, dict) and profile.get("status") == "confirmed" else {}


def meaningful(profile):
    profile = confirmed(profile)
    return bool(profile.get("cast") or str(profile.get("synopsis") or "").strip() or profile.get("events"))


def _normalized(text):
    return unicodedata.normalize("NFKC", str(text or "")).strip().casefold()


def role_aliases(profile):
    choices = defaultdict(set)
    for person in confirmed(profile).get("cast", []):
        role = str(person.get("role") or "").strip()
        if not role:
            continue
        for alias in [role, *(person.get("aliases") or [])]:
            key = _normalized(alias)
            if key:
                choices[key].add(role)
    return {key: next(iter(values)) for key, values in choices.items() if len(values) == 1}


def canonical_role(role, aliases):
    return aliases.get(_normalized(role), str(role or "").strip())


def hash_text(text):
    """Signed Unicode character/word features, with no fitted vocabulary."""
    text = _normalized(text)
    words = re.findall(r"[a-z0-9_]+|[^\W\d_]", text, flags=re.UNICODE)
    tokens = words + [left + "|" + right for left, right in zip(words, words[1:])]
    result = np.zeros(HASH_SIZE, dtype=np.float32)
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        result[int.from_bytes(digest[:4], "little") % HASH_SIZE] += 1 if digest[4] & 1 else -1
    norm = float(np.linalg.norm(result))
    return result / norm if norm else result


def encode_profile(profile, times_ms):
    times = np.asarray(times_ms)
    if times.ndim != 1 or not np.isfinite(times).all():
        raise ValueError("影片资料需要有效的一维时间轴")
    profile = confirmed(profile)
    result = np.zeros((len(times), len(NAMES)), dtype=np.float32)
    cast = "\n".join(" | ".join([str(row.get("role") or ""), str(row.get("actor") or ""),
                                 " / ".join(row.get("aliases") or [])])
                     for row in profile.get("cast", []))
    synopsis = str(profile.get("synopsis") or "").strip()
    result[:, :HASH_SIZE] = hash_text(cast)
    result[:, HASH_SIZE:HASH_SIZE * 2] = hash_text(synopsis)
    result[:, -3] = bool(cast.strip(" |\n"))
    result[:, -2] = bool(synopsis)
    events = []
    for row in profile.get("events", []):
        start, end = row.get("start_ms"), row.get("end_ms")
        if (isinstance(start, (int, float)) and isinstance(end, (int, float))
                and np.isfinite([start, end]).all() and 0 <= start < end):
            text = str(row.get("text") or "") + " " + " / ".join(row.get("roles") or [])
            if text.strip():
                events.append((start, end, text))
    for index, ms in enumerate(times):
        texts = [text for start, end, text in events if start <= ms < end]
        if texts:
            result[index, HASH_SIZE * 2:HASH_SIZE * 3] = hash_text("\n".join(texts))
            result[index, -1] = 1
    return result
