"""Money and pricing decisions use integer cents. No network or writes here."""
from decimal import Decimal, InvalidOperation
import time
import json
import math

DEFAULT_RULES = {"interval": 60, "step": 10, "maxDrop": 1000, "cooldown": 300}


def normalize_follow_count(value):
    """Old or malformed stored settings retain the original one-listing default."""
    return value if type(value) is int and 1 <= value <= 100 else 1


def listing_group_key(item):
    """Group only identifiable ordinary single spot listings, never by title."""
    archive, platform = item.get("archiveId"), item.get("platformId")
    if (archive not in (None, "") and platform not in (None, "")
        and str(item.get("goodsType")) == "1" and str(item.get("dealType")) == "0"
        and str(item.get("packageCount", 1)) in ("0", "1")):
        identity = ["single", str(archive), str(platform), "1", "0"]
    else:
        identity = ["listing", str(item.get("id"))]
    return json.dumps(identity, separators=(",", ":"), ensure_ascii=True)


def group_listings(items):
    groups = {}
    for item in items:
        groups.setdefault(listing_group_key(item), []).append(item)
    result = []
    for group_id, members in groups.items():
        # Select first, then apply rules. A paused/blocked cheapest listing must
        # never cause us to substitute a more expensive member of the group.
        representative = min(members, key=lambda x: (x["price"] if type(x.get("price")) is int and x["price"] > 0 else -1, str(x["id"])))
        row = dict(representative)
        row.update({"groupId": group_id, "quantity": len(members),
                    "memberIds": sorted(str(x["id"]) for x in members),
                    "maxPrice": max((x["price"] for x in members if type(x.get("price")) is int), default=None)})
        pending_ids = [str(x["id"]) for x in members if x.get("pending")]
        if pending_ids:
            row.update({"pending": True, "pendingIds": pending_ids,
                        "error": "同款有改价结果待核实，确认该款所有待确认挂单后再跟价"})
        result.append(row)
    return result


def default_item_setting(item, previous=None, now=None):
    """Initialize from a complete quote once; polling must not lower stored floors."""
    now = time.time() if now is None else now
    price, rival = item.get("price"), item.get("rival")
    if (item.get("saleStatus") != "on_sale" or item.get("error")
        or not item.get("marketComplete") or type(price) is not int or price <= 0
        or now - item.get("checkedAt", 0) > 240 or item.get("checkedAt", 0) > now + 5
        or (rival is not None and (type(rival) is not int or rival <= 0))):
        return None
    minimum = min(price, rival) if rival is not None else price
    market_minimum = item.get("marketMinimum", minimum)
    if type(market_minimum) is not int or not 0 < market_minimum <= minimum:
        return None
    minimum = market_minimum
    # Ceiling to a cent never makes the floor lower than the requested 80%.
    setting = {"enabled": True, "floor": max(100, (minimum * 4 + 4) // 5),
               "lastChangedAt": (previous or {}).get("lastChangedAt", 0),
               "followCount": normalize_follow_count((previous or {}).get("followCount"))}
    if "platformCooldownUntil" in (previous or {}):
        setting["platformCooldownUntil"] = previous["platformCooldownUntil"]
    return setting


def cents(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("价格格式不正确")
    try:
        price = Decimal(str(value))
        if not price.is_finite() or price <= 0 or price > 10000000 or price * 100 != (price * 100).to_integral_value():
            raise ValueError("价格必须大于 0，且最多两位小数")
        return int(price * 100)
    except InvalidOperation as exc:
        raise ValueError("价格格式不正确") from exc


def validate_rules(data):
    result = {}
    limits = {"interval": (30, 3600), "step": (1, 1000000), "maxDrop": (1, 10000000), "cooldown": (30, 86400)}
    for key, (low, high) in limits.items():
        value = data.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError("规则数值超出范围，请检查输入")
        result[key] = value
    return result


def decide(item, rules, now=None):
    return _decide(item, rules, now)


def _decide(item, rules, now=None, target_override=None):
    now = time.time() if now is None else now
    result = {"target": None, "drop": 0, "status": "blocked", "label": "暂不可跟价", "reason": ""}
    def finish(status, label, reason):
        return {**result, "status": status, "label": label, "reason": reason}
    if not item.get("enabled", False):
        return finish("paused", "未开启", "请先设置最低售价并开启跟价")
    if item.get("error"):
        return finish("blocked", "检查异常", item["error"])
    if item.get("saleStatus") != "on_sale":
        return finish("paused", "不在售", "商品已售出或不在售，跳过改价")
    if type(item.get("floor")) is not int or item["floor"] <= 0:
        return finish("blocked", "待设底价", "设置最低售价后才能跟价")
    if type(item.get("price")) is not int or item["price"] <= 0:
        return finish("blocked", "售价异常", "无法确认当前售价")
    checked = item.get("checkedAt", 0)
    if checked > now + 5 or now - checked > 240:
        return finish("blocked", "行情已过期", "请重新检查行情")
    if not item.get("marketComplete", False):
        return finish("blocked", "行情不完整", "未能确认同款挂单及本人挂单，跳过改价")
    rival = item.get("rival")
    if rival is not None and (type(rival) is not int or rival <= 0):
        return finish("blocked", "行情异常", "其他卖家价格无效")
    if target_override is None:
        if rival is None:
            return finish("lowest", "暂无其他挂单", "没有其他卖家的有效同款挂单")
        if item["price"] <= rival:
            return finish("lowest", "已是最低价", "价格相同也视为最低价，不重复降价")
        target = rival - rules["step"]
    else:
        target = target_override
        if item["price"] <= target:
            return finish("lowest", "已达跟价目标", "该挂单已达到或低于本次目标，无需改价")
    if target < 100:
        return finish("protected", "平台最低价保护", "建议价低于平台 1 元价格下限，保持原价")
    if target < item["floor"]:
        return finish("protected", "底价保护", "跟价会低于最低售价，保持原价")
    if item["price"] - target > rules["maxDrop"]:
        return finish("blocked", "降幅超限", "建议降幅超过单次上限，保持原价")
    last_changed = item.get("lastChangedAt", 0)
    platform_cooldown = rules.get("platformCooldown", 0)
    if platform_cooldown:
        platform_until = max(item.get("platformCooldownUntil", 0),
                             last_changed + platform_cooldown if last_changed else 0)
        if now < platform_until:
            remaining = math.ceil(max(platform_until, last_changed + rules["cooldown"]) - now)
            return finish("paused", "平台改价冷却中",
                          f"平台改价冷却中，每件 30 分钟内只能改价一次；本款还需等待 {remaining} 秒后再检查跟价")
    if now - last_changed < rules["cooldown"]:
        return finish("paused", "改价冷却中", "等待冷却时间结束后再跟价")
    stage_target = max(target, (item["price"] + 4) // 5)
    if stage_target > target:
        return {**result, "target": stage_target, "finalTarget": target,
                "drop": item["price"] - stage_target, "status": "action", "label": "需分次跟价",
                "reason": "平台单次改价不得低于原价的 20%；本次先降至允许价，冷却后再检查跟价"}
    return {**result, "target": target, "drop": item["price"] - target, "status": "action", "label": "发现更低价", "reason": "可在最低售价范围内一键跟价"}


def plan_following(group, members, rules, now=None):
    """Choose the cheapest N first, then protect each listing at one common target.

    Group settings and failures apply to every selected listing. Individual quote
    errors and cooldowns are also retained. Protected or already-lowest members
    consume a slot; a more expensive listing must never be silently substituted.
    The returned dictionaries do not alias or modify any input.
    """
    now = time.time() if now is None else now
    count = normalize_follow_count(group.get("followCount"))
    selected = sorted(members, key=lambda item: (
        item["price"] if type(item.get("price")) is int and item["price"] > 0 else -1,
        str(item["id"]),
    ))[:count]
    base = decide(group, rules, now)
    group_price, rival = group.get("price"), group.get("rival")
    target = None
    if (type(group_price) is int and group_price > 0
        and (rival is None or (type(rival) is int and rival > 0))):
        target = group_price if rival is None or group_price <= rival else rival - rules["step"]

    planned = []
    for member in selected:
        item = dict(member)
        for name in ("enabled", "floor", "rival"):
            item[name] = group.get(name)
        item["lastChangedAt"] = max(group.get("lastChangedAt", 0), member.get("lastChangedAt", 0))
        item["platformCooldownUntil"] = max(group.get("platformCooldownUntil", 0), member.get("platformCooldownUntil", 0))
        item["marketComplete"] = bool(group.get("marketComplete") and member.get("marketComplete"))
        group_checked, member_checked = group.get("checkedAt", 0), member.get("checkedAt", 0)
        # A future timestamp must not disappear when choosing the older quote.
        item["checkedAt"] = (max(group_checked, member_checked)
                             if max(group_checked, member_checked) > now + 5
                             else min(group_checked, member_checked))
        item["error"] = group.get("error") or member.get("error")
        if group.get("pending") or member.get("pending"):
            item["error"] = item["error"] or "同款有改价结果待核实，请确认后再跟价"
        if group.get("saleStatus") != "on_sale":
            item["saleStatus"] = group.get("saleStatus")
        # An invalid group representative cannot produce a safe shared target.
        decision = (dict(base) if target is None
                    else decide(item, rules, now) if count == 1
                    else _decide(item, rules, now, target_override=target))
        planned.append({"id": member["id"], "price": member.get("price"), "decision": decision})

    actions = [item for item in planned if item["decision"]["status"] == "action"]
    if count == 1 and planned:
        decision = dict(planned[0]["decision"])
    elif actions:
        decision = {"target": target, "drop": sum(item["decision"]["drop"] for item in actions),
                    "status": "action", "label": f"可跟价 {len(actions)} 件",
                    "reason": f"按售价从低到高选取 {len(planned)} 件，其中 {len(actions)} 件可调整至统一最低价"}
        if any("finalTarget" in item["decision"] for item in actions):
            decision.update({"finalTarget": target, "label": f"分次跟价 {len(actions)} 件",
                             "reason": "部分挂单受平台单次 20% 下限限制，分别先降至允许价；冷却后可继续向最终目标跟价"})
    elif planned:
        decisions = [item["decision"] for item in planned]
        decision = dict(min(decisions, key=lambda value: {
            "blocked": 0, "protected": 1, "paused": 2, "lowest": 3,
        }.get(value["status"], 4)))
    else:
        decision = {"target": None, "drop": 0, "status": "blocked",
                    "label": "无在售挂单", "reason": "没有可选的同款在售挂单，请重新读取我的出售"}
    return {"followCount": count, "followAvailable": len(selected), "followItems": planned,
            "actionCount": len(actions), "decision": decision}
