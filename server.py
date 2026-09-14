"""Loopback-only local dashboard. Browser credentials never enter HTTP responses."""
import argparse
import concurrent.futures
import copy
import json
import importlib
from pathlib import Path
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from pricing import DEFAULT_RULES, decide, default_item_setting, group_listings, listing_group_key, normalize_follow_count, plan_following, validate_rules
from xmeta_adapter import PlatformConnectionError

ROOT = Path(__file__).resolve().parent
DATA = ROOT / ".local"


class BatchQuoteExpired(ValueError):
    stop_batch = True


class Service:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = queue.Queue()
        self.rules = dict(DEFAULT_RULES)
        self.settings = {}
        self.mode = "demo"
        self.running = False
        self.busy = False
        self.next_check = 0
        self.last_check = 0
        self.error = ""
        self.logs = []
        self.adapter = None
        self.pending = {}
        self.pending_groups = {}
        self.round = 0
        self.batch = None
        self.batch_plans = {}
        self.read_config()
        self.items = self.demo_items()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def read_config(self):
        try:
            data = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
            self.rules = validate_rules(data["rules"])
            self.settings = data.get("items", {})
            self.pending = data.get("pending", {})
            self.pending_groups = data.get("pendingGroups", {})
            if not isinstance(self.pending_groups, dict):
                self.pending_groups = {}
            if not isinstance(self.settings, dict) or not isinstance(self.pending, dict):
                self.settings, self.pending = {}, {}
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def save_config(self):
        DATA.mkdir(exist_ok=True)
        temporary = DATA / "config.tmp"
        temporary.write_text(json.dumps({"rules": self.rules, "items": self.settings, "pending": self.pending, "pendingGroups": self.pending_groups}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(DATA / "config.json")

    def demo_items(self):
        now = time.time()
        return [{"id": f"demo-{i}", "archiveId": f"demo-a-{i}", "name": name, "serial": serial,
                 "platformId": "demo", "goodsType": 1, "dealType": 0, "packageCount": 1,
                 "price": price, "rival": rival, "floor": floor, "enabled": True,
                 "saleStatus": "on_sale", "marketComplete": True, "checkedAt": now, "lastChangedAt": 0}
                for i, (name, serial, price, rival, floor) in enumerate([
                    ("云上山河 · 青岚", "示例 / 001", 12800, 12600, 11000),
                    ("星际漫游 · 探索者", "示例 / 002", 8900, 8600, 8000),
                    ("东方意境 · 竹影", "示例 / 003", 6500, 6500, 5500),
                    ("时间藏馆 · 流光", "示例 / 004", 21000, 18800, 20000),
                ])]

    def log(self, title, detail="", level="info"):
        self.logs.insert(0, {"time": time.time(), "title": title, "detail": detail, "level": level, "mode": self.mode})
        self.logs = self.logs[:200]

    def group_setting_key(self, group_id):
        scope = self.adapter.account_scope if self.mode == "live" and self.adapter else "demo"
        return f"{scope}:group:{group_id}"

    def pricing_rules(self):
        # Keep the user's saved preference; the live platform enforces its own
        # minimum between separate operations. Demo behavior stays unchanged.
        return {**self.rules, "platformCooldown": 1800} if self.mode == "live" else self.rules

    def pending_keys_for_group(self, group):
        if self.mode != "live" or not self.adapter:
            return []
        prefix = f"{self.adapter.account_scope}:"
        members = {prefix + member_id for member_id in group["memberIds"]}
        legacy_unknown = set(self.unassigned_pending_keys())
        return [key for key in self.pending if key.startswith(prefix)
                and (key in members or self.pending_groups.get(key) == group["groupId"] or key in legacy_unknown)]

    def unassigned_pending_keys(self):
        if self.mode != "live" or not self.adapter:
            return []
        prefix = f"{self.adapter.account_scope}:"
        return [key for key in self.pending if key.startswith(prefix) and not self.pending_groups.get(key)]

    def grouped_items(self, now=None):
        rows = group_listings(self.items)
        by_id = {item["id"]: item for item in self.items}
        for row in rows:
            if self.mode == "live":
                setting = self.settings.get(self.group_setting_key(row["groupId"]), {})
                row["platformCooldownUntil"] = max([setting.get("platformCooldownUntil", 0)] + [
                    by_id[item_id].get("platformCooldownUntil", 0) for item_id in row["memberIds"]])
            pending_keys = self.pending_keys_for_group(row)
            if pending_keys:
                prefix = f"{self.adapter.account_scope}:"
                row.update({"pending": True, "pendingIds": [key[len(prefix):] for key in pending_keys],
                            "error": "同款有改价结果待核实，确认该款所有待确认挂单后再跟价"})
                unknown = self.unassigned_pending_keys()
                if unknown:
                    row["unassignedPendingIds"] = [key[len(prefix):] for key in unknown]
                    row["error"] = "旧版待确认挂单无法归入同款，请先到平台核实，再在设置中解除锁定"
            row.update(plan_following(row, [by_id[item_id] for item_id in row["memberIds"]], self.pricing_rules(), now))
        return rows

    def require_representative(self, item_id):
        group = next((row for row in self.grouped_items() if row["id"] == item_id), None)
        if group is None:
            raise ValueError("同款最低价挂单已变化或所选并非最低价那件，请刷新后选择当前展示的挂单")
        return group

    def snapshot(self):
        with self.lock:
            now = time.time()
            items = copy.deepcopy(self.grouped_items(now))
            return {"app": "xmeta-local-pricing", "mode": self.mode, "running": self.running, "busy": self.busy, "rules": self.rules.copy(),
                    "items": items, "listingTotal": len(self.items), "logs": copy.deepcopy(self.logs), "lastCheck": self.last_check,
                    "nextCheck": self.next_check, "serverTime": now, "error": self.error,
                    "browserOpen": bool(self.adapter and self.adapter.is_open()), "batch": copy.deepcopy(self.batch)}

    def call(self, name, data):
        future = concurrent.futures.Future()
        self.jobs.put((name, data, future))
        return future.result(timeout=360)

    def loop(self):
        while True:
            try:
                name, data, future = self.jobs.get(timeout=.25)
            except queue.Empty:
                if self.running and not self.batch_active() and time.time() >= self.next_check:
                    try:
                        self.check()
                    except Exception:
                        pass
                continue
            try:
                result = self.command(name, data)
                if future is not None:
                    future.set_result(result)
            except Exception as exc:
                message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "操作失败，请检查平台登录状态后重试"
                if future is not None:
                    future.set_exception(ValueError(message))
                else:
                    self.cancel_batch("批量任务异常，未继续提交", failed=True)

    def check(self, advance_demo=True, initialize=False):
        with self.lock:
            self.busy = True
            self.error = ""
        try:
            if self.mode == "demo":
                self.round += 1 if advance_demo else 0
                fresh = copy.deepcopy(self.items)
                for item in fresh:
                    item["checkedAt"] = time.time()
                # Explicit deterministic demo quotes; never presented as real market data.
                if advance_demo:
                    fresh[0]["rival"] = [12600, 12550, 12700, 12650][(self.round - 1) % 4]
            else:
                if not self.adapter:
                    raise ValueError("请先打开专用登录窗口并登录 Xmeta")
                fresh = self.adapter.read_items()
                settings_changed = False
                by_id = {item["id"]: item for item in fresh}
                for item in fresh:
                    key = f"{self.adapter.account_scope}:{item['id']}"
                    if key in self.pending:
                        item["pending"] = True
                        item["error"] = "上次改价结果待核实。请到平台确认后，在商品设置中解除锁定。"
                        if key not in self.pending_groups:
                            self.pending_groups[key] = listing_group_key(item)
                            settings_changed = True
                for group in group_listings(fresh):
                    group_key = self.group_setting_key(group["groupId"])
                    legacy = self.settings.get(f"{self.adapter.account_scope}:{group['id']}", {})
                    setting = dict(self.settings.get(group_key, legacy))
                    last_changed = max([setting.get("lastChangedAt", 0)] + [
                        self.settings.get(f"{self.adapter.account_scope}:{member_id}", {}).get("lastChangedAt", 0)
                        for member_id in group["memberIds"]])
                    if setting:
                        setting["lastChangedAt"] = last_changed
                    platform_until = max([setting.get("platformCooldownUntil", 0)] + [
                        self.settings.get(f"{self.adapter.account_scope}:{member_id}", {}).get("platformCooldownUntil", 0)
                        for member_id in group["memberIds"]])
                    if platform_until:
                        setting["platformCooldownUntil"] = platform_until
                    if (initialize or not setting) and not self.pending_keys_for_group(group):
                        automatic = default_item_setting(group, setting)
                        if automatic is not None:
                            automatic["lastChangedAt"] = last_changed
                            setting = automatic
                    if setting:
                        setting["followCount"] = normalize_follow_count(setting.get("followCount", 1))
                    if setting and self.settings.get(group_key) != setting:
                        self.settings[group_key] = dict(setting)
                        settings_changed = True
                    for member_id in group["memberIds"]:
                        item = by_id[member_id]
                        key = f"{self.adapter.account_scope}:{member_id}"
                        item.update({"floor": setting.get("floor"), "enabled": setting.get("enabled", False),
                                     "followCount": normalize_follow_count(setting.get("followCount", 1)),
                                     "platformCooldownUntil": setting.get("platformCooldownUntil", 0),
                                     "lastChangedAt": max(setting.get("lastChangedAt", 0), self.settings.get(key, {}).get("lastChangedAt", 0))})
                        if setting and self.settings.get(key) != setting:
                            self.settings[key] = dict(setting)
                            settings_changed = True
                if settings_changed:
                    self.save_config()
            with self.lock:
                self.items = fresh
                self.last_check = time.time()
                groups = self.grouped_items()
                count = sum(x["decision"]["status"] == "action" for x in groups)
                pieces = sum(x["actionCount"] for x in groups)
                self.log("演示检查完成" if self.mode == "demo" else "行情检查完成", f"共 {len(fresh)} 件、{len(groups)} 款；{count} 款、{pieces} 件可以跟价。")
                if initialize and self.mode == "live":
                    self.log("已自动设置商品跟价", "可比价商品已开启，底价设为同款当前最低挂单价的 80%；定时检查不重设底价。")
        except Exception as exc:
            with self.lock:
                self.error = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "读取失败，请重新登录或稍后重试"
                for item in self.items:
                    item["marketComplete"] = False
                    item["error"] = self.error
                self.running = False
                self.log("检查已暂停", self.error, "warning")
            raise ValueError(self.error) from None
        finally:
            with self.lock:
                self.busy = False
                self.next_check = time.time() + self.rules["interval"] if self.running else 0

    def command(self, name, data):
        if self.batch_active() and name not in ("_batch_step", "cancel_batch", "stop", "close"):
            raise ValueError("批量跟价正在进行，请等待完成或停止剩余商品")
        if name == "check":
            self.check()
        elif name == "start":
            self.running = True
            self.check()
        elif name == "stop":
            self.cancel_batch("你已暂停监控，剩余商品未提交")
            self.running = False
            self.next_check = 0
            self.log("自动检查已暂停")
        elif name == "rules":
            self.rules = validate_rules(data)
            self.save_config()
            self.next_check = time.time() + self.rules["interval"] if self.running else 0
            self.log("跟价规则已保存", "自动检查只生成建议，改价由你点击执行。")
        elif name == "item":
            group = self.require_representative(data.get("id"))
            floor = data.get("floor")
            if type(floor) is not int or not 100 <= floor <= 1000000000 or type(data.get("enabled")) is not bool:
                raise ValueError("请填写有效的最低售价")
            follow_count = data.get("followCount", group["followCount"])
            if type(follow_count) is not int or not 1 <= follow_count <= 100:
                raise ValueError("跟价件数须为 1 至 100 的整数")
            members = [x for x in self.items if x["id"] in group["memberIds"]]
            for item in members:
                item.update({"floor": floor, "enabled": data["enabled"], "followCount": follow_count})
            if self.mode == "live":
                if data.get("resolvePending") is True:
                    for key in self.pending_keys_for_group(group):
                        self.pending.pop(key, None)
                        self.pending_groups.pop(key, None)
                    for item in members:
                        item.pop("error", None)
                        item["marketComplete"] = False
                        item["pending"] = False
                    self.log("已人工解除待确认锁定", "请重新检查行情后再跟价。")
                setting = {"floor": floor, "enabled": data["enabled"], "followCount": follow_count,
                           "lastChangedAt": max(x.get("lastChangedAt", 0) for x in members)}
                if group.get("platformCooldownUntil"):
                    setting["platformCooldownUntil"] = group["platformCooldownUntil"]
                self.settings[self.group_setting_key(group["groupId"])] = dict(setting)
                for item in members:
                    self.settings[f"{self.adapter.account_scope}:{item['id']}"] = dict(setting)
                self.save_config()
            self.log("商品设置已保存", f"{group['name']}：按售价从低到高选 {follow_count} 件跟价，已在最低价的件也计入。")
        elif name == "reset":
            if self.mode != "demo":
                raise ValueError("请先切换到演示环境")
            self.running = False
            self.next_check = 0
            self.items = self.demo_items()
            self.batch = None
            self.logs = []
            self.log("演示已重置", "示例数据不会影响 Xmeta 账号。")
        elif name == "login":
            if self.adapter is None:
                from xmeta_adapter import XmetaAdapter
                self.adapter = XmetaAdapter(DATA)
            self.adapter.open_login()
            self.log("已打开专用登录窗口", "请在 Xmeta 官方页面完成登录，然后点击读取我的出售。")
        elif name == "mode":
            mode = data.get("mode")
            if mode not in ("demo", "live"):
                raise ValueError("运行模式不正确")
            initialize = data.get("initialize", True)
            if type(initialize) is not bool:
                raise ValueError("读取选项不正确")
            self.running = False
            self.next_check = 0
            self.mode = mode
            self.batch = None
            self.items = self.demo_items() if mode == "demo" else []
            self.error = ""
            self.last_check = 0
            self.check(initialize=mode == "live" and initialize)
        elif name == "apply":
            if self.require_representative(data.get("id"))["followCount"] > 1:
                self.begin_batch({"mode": self.mode, "items": [data]})
            else:
                self.apply_price(data)
        elif name == "batch_apply":
            self.begin_batch(data)
        elif name == "_batch_step":
            self.batch_step()
        elif name == "cancel_batch":
            self.cancel_batch("你已停止剩余商品，已提交的改价结果保留")
        elif name == "diagnose_price_limits":
            if self.mode != "live" or not self.adapter:
                raise ValueError("请先连接 Xmeta 并读取我的出售")
            item = next((x for x in self.items if x["id"] == data.get("id")), None)
            if not item:
                raise ValueError("商品不在当前出售列表")
            limits = self.adapter.post("/h5/mySell/priceLimits", {"archiveId": item["archiveId"]})
            fields = ("priceStatus", "minGoodsPrice", "maxGoodsPrice", "beforeDealPrice", "increaseStatus", "minIncrease", "maxIncrease")
            return {"dataType": type(limits).__name__, "limits": {key: limits[key] for key in fields if key in limits} if isinstance(limits, dict) else None}
        elif name == "diagnose_listing_confirmation":
            # Read-only maintenance check against the last observed asking price.
            if self.mode != "live" or not self.adapter:
                raise ValueError("请先连接 Xmeta 并读取我的出售")
            item = self.require_representative(data.get("id"))
            started = time.monotonic()
            self.adapter.confirm_update(item, item["price"])
            return {"verified": True, "elapsedMs": round((time.monotonic() - started) * 1000)}
        elif name == "diagnose_pending":
            # Read only. Report existing locked IDs and payment-in-progress listings;
            # this never clears a lock or submits a payment/price change.
            if self.mode != "live" or not self.adapter or not self.adapter.account_scope:
                raise ValueError("请先连接 Xmeta 并读取我的出售")
            import hashlib
            scope = self.adapter.account_scope
            details = []
            for key, target in self.pending.items():
                if not key.startswith(scope + ":"):
                    continue
                entry = {"id": key.split(":", 1)[1], "target": target, "groupId": self.pending_groups.get(key)}
                try:
                    detail = self.adapter.post("/h5/goods/details", {"goodsId": entry["id"]})
                    if not isinstance(detail, dict):
                        raise ValueError("挂单详情格式异常")
                    entry["sellerMatches"] = hashlib.sha256(str(detail.get("uid")).encode()).hexdigest()[:24] == scope
                    entry["detail"] = {field: detail.get(field) for field in ("id", "name", "archiveId", "platformId", "amount", "sellStatus", "goodsType", "dealType", "packageCount", "isPayBond", "earnestAmount")}
                except PlatformConnectionError:
                    raise
                except ValueError as exc:
                    if getattr(exc, "stop_batch", False):
                        raise
                    entry["error"] = str(exc)
                details.append(entry)
            paying = self.adapter.pages("/h5/mySell/listAppNew", {"platformId": None, "orderVal": "", "goodsStatus": 2}, "goods", "pageNum", "id", 100)
            if self.adapter.account_scope != scope:
                raise PlatformConnectionError("核实时账号发生变化")
            return {"pending": details, "paymentListings": [{field: item.get(field) for field in ("id", "name", "archiveId", "amount", "businessType", "count", "paidGoodsEarnestAmount")} for item in paying]}
        elif name == "reload_adapter":
            # Maintenance only: reload fixed local code while preserving the login context.
            # This command never checks, schedules, or submits a price change.
            module = importlib.reload(importlib.import_module("xmeta_adapter"))
            if self.adapter:
                self.adapter.__class__ = module.XmetaAdapter
            self.log("平台适配已更新", "现有登录窗口保持连接，改价仍需你点击。")
        elif name == "close":
            self.cancel_batch("服务已关闭，剩余商品未提交")
            self.running = False
            if self.adapter:
                self.adapter.close()
        else:
            raise ValueError("未知操作")
        return self.snapshot()

    def batch_active(self):
        return bool(self.batch and self.batch["status"] in ("queued", "running"))

    @staticmethod
    def candidate_signature(group):
        return [{"id": item["id"], "price": item["price"], "target": item["decision"]["target"]}
                for item in group["followItems"]]

    def validate_group_request(self, group, request):
        count = request.get("followCount", 1)
        if type(count) is not int or count != group["followCount"]:
            raise ValueError("跟价件数已变化，请刷新页面并核对后重新选择")
        if group["decision"]["status"] != "action":
            raise ValueError(group["decision"]["reason"])
        if group["price"] != request.get("price") or group["decision"]["target"] != request.get("target"):
            raise ValueError("所选商品价格已变化，请核对最新建议后重新选择")
        candidates = request.get("candidates")
        if count > 1 or candidates is not None:
            if (not isinstance(candidates, list) or any(not isinstance(x, dict) or
                type(x.get("price")) is not int or (x.get("target") is not None and type(x["target"]) is not int)
                for x in candidates) or candidates != self.candidate_signature(group)):
                raise ValueError("所选挂单或价格已变化，请刷新页面并核对跟价明细")

    def begin_batch(self, data):
        requests = data.get("items")
        if data.get("mode") != self.mode:
            raise ValueError("运行环境已变化，请刷新面板后重新选择")
        if not isinstance(requests, list) or not 1 <= len(requests) <= 100:
            raise ValueError("请勾选 1 至 100 款可跟价商品")
        rows, plans, seen, seen_groups = [], {}, set(), set()
        for request in requests:
            if not isinstance(request, dict):
                raise ValueError("批量请求格式不正确")
            item_id, price, target = request.get("id"), request.get("price"), request.get("target")
            if not isinstance(item_id, str) or item_id in seen:
                raise ValueError("批量商品标识不正确或出现重复")
            if type(price) is not int or type(target) is not int or not 100 <= target <= price:
                raise ValueError("批量价格确认信息不正确")
            item = self.require_representative(item_id)
            if request.get("groupId", item["groupId"]) != item["groupId"]:
                raise ValueError("商品归属已变化，请重新选择")
            if item["groupId"] in seen_groups:
                raise ValueError("同一款商品每批只能选择一次")
            self.validate_group_request(item, request)
            seen.add(item_id)
            seen_groups.add(item["groupId"])
            plans[item["groupId"]] = {"request": {"id": item_id, "groupId": item["groupId"], "price": price,
                "target": target, "followCount": item["followCount"], "candidates": self.candidate_signature(item)},
                "validated": False, "lastSuccessAt": None}
            for candidate in item["followItems"]:
                if candidate["decision"]["status"] != "action":
                    continue
                rows.append({"id": candidate["id"], "groupId": item["groupId"], "name": item["name"],
                             "price": candidate["price"], "target": candidate["decision"]["target"],
                             "status": "queued", "message": "等待处理"})
        if len(rows) > 1000:
            raise ValueError("本批需要改价超过 1000 件，请减少所选商品或跟价件数")
        requested_ids = {row["id"] for row in rows}
        # Keep each quote's original age; clicking apply must not restart its lifetime.
        quote_expires_at = min(item.get("checkedAt", 0) for item in self.items
                               if item["id"] in requested_ids) + 240
        if time.time() >= quote_expires_at:
            raise BatchQuoteExpired("行情已超过 240 秒有效期，请先点击立即检查后再跟价")
        self.batch_plans = plans
        self.batch = {"id": secrets.token_hex(8), "mode": self.mode, "status": "queued", "items": rows,
                      "accountScope": self.adapter.account_scope if self.mode == "live" and self.adapter else None,
                      "total": len(rows), "groupTotal": len(plans), "completed": 0, "succeeded": 0, "skipped": 0, "failed": 0,
                      "startedAt": time.time(), "finishedAt": None, "quotesReady": True, "quoteExpiresAt": quote_expires_at}
        self.log("批量演示跟价已开始" if self.mode == "demo" else "批量跟价已开始", f"共 {len(plans)} 款、{len(rows)} 件需要改价；复用最近检查的有效行情，按各款设定件数执行。")
        self.jobs.put(("_batch_step", {}, None))

    def batch_member(self, data):
        """Resolve a frozen member; only this batch's own successes waive cooldown."""
        self.check_batch_quote_age()
        if not any(row["status"] == "running" and all(row[key] == data.get(key)
                   for key in ("id", "groupId", "price", "target")) for row in self.batch["items"]):
            raise ValueError("挂单不在当前批量计划中")
        plan = self.batch_plans.get(data.get("groupId"))
        if not plan:
            raise ValueError("商品跟价计划已变化")
        if plan.get("error"):
            raise ValueError(plan["error"])
        if not plan["validated"]:
            try:
                group = self.require_representative(plan["request"]["id"])
                if group["groupId"] != data["groupId"]:
                    raise ValueError("商品归属已变化，未提交改价")
                self.validate_group_request(group, plan["request"])
                plan["lastChangedAt"] = group.get("lastChangedAt", 0)
                plan["validated"] = True
            except ValueError as exc:
                plan["error"] = str(exc)
                raise
        group = next((x for x in self.grouped_items() if x["groupId"] == data["groupId"]), None)
        if not group or group["followCount"] != plan["request"]["followCount"]:
            raise ValueError("该款挂单或跟价件数已变化")
        members = [dict(x) for x in self.items if x["id"] in group["memberIds"]]
        if plan["lastSuccessAt"] is not None and group.get("lastChangedAt", 0) == plan["lastSuccessAt"]:
            group = {**group, "lastChangedAt": plan["lastChangedAt"]}
            for member in members:
                if member.get("lastChangedAt", 0) == plan["lastSuccessAt"]:
                    member["lastChangedAt"] = plan["lastChangedAt"]
        evaluated = plan_following(group, members, self.pricing_rules())
        candidate = next((x for x in evaluated["followItems"] if x["id"] == data["id"]), None)
        if not candidate or candidate["price"] != data["price"]:
            raise ValueError("计划中的挂单或售价已变化，不会替换为其他件")
        decision = candidate["decision"]
        if decision["status"] != "action":
            raise ValueError(decision["reason"])
        if decision["target"] != data["target"]:
            raise ValueError("该件建议价已变化，请重新核对")
        item = next(x for x in self.items if x["id"] == data["id"])
        return group, item, decision

    def check_batch_quote_age(self):
        if not self.batch_active() or not self.batch.get("quotesReady"):
            raise ValueError("批量行情尚未准备完成，未提交改价")
        if time.time() >= self.batch["quoteExpiresAt"]:
            raise BatchQuoteExpired("本批行情已超过 240 秒有效期，剩余商品未提交，请重新检查后选择")

    def batch_step(self):
        if not self.batch_active():
            return
        row = next((x for x in self.batch["items"] if x["status"] == "queued"), None)
        if row is None:
            self.finish_batch()
            return
        self.batch["status"] = "running"
        row["status"] = "running"
        row["message"] = "正在按最近检查的行情改价"
        if (self.mode != self.batch["mode"] or (self.mode == "live" and
            (not self.adapter or self.adapter.account_scope != self.batch["accountScope"]))):
            row["status"], row["message"] = "failed", "运行环境已变化，未提交"
            self.cancel_batch(row["message"], failed=True)
            return
        try:
            self.check_batch_quote_age()
            row["message"] = "正在按本批行情改价并确认结果"
            self.apply_price({key: row[key] for key in ("id", "groupId", "price", "target")}, batch=True)
            row["status"], row["message"] = "succeeded", "已确认改价成功" if self.mode == "live" else "演示跟价完成"
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else "处理异常，请检查记录"
            key = f"{self.batch['accountScope']}:{row['id']}"
            uncertain = key in self.pending
            account_changed = self.mode == "live" and (not self.adapter or self.adapter.account_scope != self.batch["accountScope"])
            must_stop = uncertain or account_changed or bool(self.error) or getattr(exc, "stop_batch", False) or not isinstance(exc, ValueError)
            row["status"] = "failed" if must_stop else "skipped"
            row["message"] = message
            self.log("批量商品处理失败" if must_stop else "批量商品已跳过", f"{row['name']}：{message}", "warning")
            if must_stop:
                reason = message if isinstance(exc, BatchQuoteExpired) else "上一件结果待核实或连接异常，剩余商品未提交"
                self.cancel_batch(reason, failed=True)
                return
        self.update_batch_counts()
        if any(x["status"] == "queued" for x in self.batch["items"]):
            self.jobs.put(("_batch_step", {}, None))
        else:
            self.finish_batch()

    def update_batch_counts(self):
        if not self.batch:
            return
        for status in ("succeeded", "skipped", "failed"):
            self.batch[status] = sum(x["status"] == status for x in self.batch["items"])
        self.batch["completed"] = sum(x["status"] not in ("queued", "running") for x in self.batch["items"])

    def finish_batch(self):
        self.update_batch_counts()
        self.batch["status"] = "completed"
        self.batch["finishedAt"] = time.time()
        self.next_check = self.batch["finishedAt"] + self.rules["interval"] if self.running else 0
        self.log("批量跟价处理完成", f"成功 {self.batch['succeeded']} 件，跳过 {self.batch['skipped']} 件，失败 {self.batch['failed']} 件。")

    def cancel_batch(self, reason, failed=False):
        if not self.batch_active():
            return
        for row in self.batch["items"]:
            if row["status"] == "queued":
                row["status"], row["message"] = "not_run", reason
            elif row["status"] == "running" and failed:
                row["status"], row["message"] = "failed", reason
        self.batch["status"] = "failed" if failed else "cancelled"
        if failed:
            self.running = False
            self.next_check = 0
        self.batch["finishedAt"] = time.time()
        self.next_check = self.batch["finishedAt"] + self.rules["interval"] if self.running else 0
        self.update_batch_counts()
        self.log("批量跟价已停止", reason, "warning" if failed else "info")

    def apply_price(self, data, *, batch=False):
        expected_scope = self.adapter.account_scope if self.mode == "live" and self.adapter else None
        item_id = data.get("id")
        expected = data.get("target")
        original = data.get("price")
        if type(expected) is not int or type(original) is not int:
            raise ValueError("价格确认信息不完整，请重新检查")
        # Both paths use the latest checked quotes. Only the internal batch actor
        # can select a frozen member and waive this batch's own new cooldown.
        if not batch:
            group = self.require_representative(item_id)
            if group["groupId"] != data.get("groupId", group["groupId"]):
                raise ValueError("商品归属已变化，未提交改价")
            if group["followCount"] != 1:
                raise ValueError("跟价件数已变化，请刷新后按新的件数执行")
            self.validate_group_request(group, data)
            item = next(x for x in self.items if x["id"] == item_id)
            decision = group["decision"]
        else:
            group, item, decision = self.batch_member(data)
        if self.mode == "live" and (not self.adapter or self.adapter.account_scope != expected_scope):
            raise PlatformConnectionError("当前账号发生变化，请重新读取出售列表后再跟价")
        if decision["status"] != "action":
            raise ValueError(decision["reason"])
        if item["price"] != original or decision["target"] != expected:
            raise ValueError("行情或售价已变化，未提交改价。请核对最新建议后再点击。")
        if self.mode == "live":
            self.adapter.prepare_update(item, expected)
            if self.adapter.account_scope != expected_scope:
                raise PlatformConnectionError("提交前账号发生变化，未提交改价")
            if batch:
                group, item, decision = self.batch_member(data)
            elif self.require_representative(item_id)["decision"]["status"] != "action":
                raise ValueError("提交前行情已过期，请重新检查")
            # Mark pending before the network write. Ambiguous responses must never be retried automatically.
            key = f"{expected_scope}:{item_id}"
            if key in self.pending:
                raise ValueError("上次改价结果待确认，请到平台核实后重新连接，避免重复提交")
            self.pending[key] = expected
            self.pending_groups[key] = group["groupId"]
            self.save_config()
            update_accepted = False
            try:
                self.adapter.update_price(item, expected)
                update_accepted = True
                self.adapter.confirm_update(item, expected)
                if self.adapter.account_scope != expected_scope:
                    raise PlatformConnectionError("改价后账号发生变化，无法确认最终售价，请到平台核实")
            except Exception as exc:
                if (not update_accepted and getattr(exc, "definitive_rejection", False)
                    and self.adapter.account_scope == expected_scope):
                    self.pending.pop(key, None)
                    self.pending_groups.pop(key, None)
                    platform_wait = getattr(exc, "platform_cooldown_seconds", 0)
                    if type(platform_wait) is int and platform_wait > 0:
                        group_key = self.group_setting_key(group["groupId"])
                        setting = dict(self.settings.get(group_key, {}))
                        until = max(setting.get("platformCooldownUntil", 0), time.time() + platform_wait)
                        setting["platformCooldownUntil"] = until
                        self.settings[group_key] = setting
                        for member in self.items:
                            if listing_group_key(member) == group["groupId"]:
                                member["platformCooldownUntil"] = until
                                member_key = f"{expected_scope}:{member['id']}"
                                self.settings.setdefault(member_key, dict(setting))["platformCooldownUntil"] = until
                    self.save_config()
                    self.log("平台拒绝本次改价", f"{item['name']}：{exc}", "warning")
                    raise ValueError(str(exc)) from None
                self.running = False
                self.next_check = 0
                item["error"] = "改价结果待核实，请到平台确认"
                item["pending"] = True
                self.log("改价需要人工核实", f"{item['name']}：{str(exc) if isinstance(exc, ValueError) else '请求未完成'}", "warning")
                raise ValueError("改价未确认完成，请到 Xmeta 检查最终售价及是否需要补保证金。") from None
            self.pending.pop(key, None)
            self.pending_groups.pop(key, None)
        item["price"] = expected
        item["lastChangedAt"] = time.time()
        for member in self.items:
            if listing_group_key(member) == group["groupId"]:
                member["lastChangedAt"] = item["lastChangedAt"]
        if batch:
            self.batch_plans[group["groupId"]]["lastSuccessAt"] = item["lastChangedAt"]
        if self.mode == "live":
            self.settings[key]["lastChangedAt"] = item["lastChangedAt"]
            self.settings[self.group_setting_key(group["groupId"])]["lastChangedAt"] = item["lastChangedAt"]
            self.save_config()
        prefix = "演示跟价完成" if self.mode == "demo" else "已确认改价成功"
        self.log(prefix, f"{item['name']}：¥{original/100:.2f} → ¥{expected/100:.2f}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, content, mime="application/json; charset=utf-8"):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(content)

    def trusted_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def do_GET(self):
        if not self.trusted_host():
            return self.send(403, {"error": "仅允许本机访问"})
        path = urlsplit(self.path).path
        if path == "/api/state":
            return self.send(200, {**self.server.service.snapshot(), "csrf": self.server.csrf})
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/index.html": ("index.html", "text/html; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}
        if path not in files:
            return self.send(404, {"error": "页面不存在"})
        filename, mime = files[path]
        try:
            self.send(200, (ROOT / "dist" / filename).read_bytes(), mime)
        except OSError:
            self.send(404, {"error": "文件不存在"})

    def discard_small_rejected_body(self):
        # Closing with unread bytes can reset the connection on Windows before
        # the client receives its rejection. Drain only a small, explicit body,
        # with one overall deadline so an incomplete request cannot stall us.
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
            return
        try:
            remaining = int(lengths[0])
        except ValueError:
            return
        if not 0 < remaining <= 65536:
            return
        previous_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + .2
        try:
            while remaining:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                self.connection.settimeout(wait)
                chunk = self.rfile.read1(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def do_POST(self):
        origin = self.headers.get("Origin")
        expected = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if not self.trusted_host() or origin not in expected or not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf):
            self.discard_small_rejected_body()
            return self.send(403, {"error": "请求来源不正确，请从本机面板操作"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.discard_small_rejected_body()
            return self.send(415, {"error": "请求格式不正确"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1048576:
                raise ValueError("请求长度不正确")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求格式不正确")
            path = urlsplit(self.path).path
            if not path.startswith("/api/") or path[5:].startswith("_"):
                return self.send(404, {"error": "未知操作"})
            if path == "/api/shutdown":
                self.server.service.call("close", {})
                self.send(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            result = self.server.service.call(path[5:], data)
            self.send(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send(400, {"error": str(exc)})
        except concurrent.futures.TimeoutError:
            self.send(504, {"error": "操作仍在处理中，请稍后检查记录；请勿重复提交改价"})
        except Exception:
            self.send(500, {"error": "本机服务异常，请稍后重试"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.csrf = secrets.token_urlsafe(32)
    server.service = Service()
    print(f"Xmeta local dashboard: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
