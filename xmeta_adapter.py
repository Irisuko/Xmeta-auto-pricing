"""Xmeta adapter based on public frontend sources; strict schema checks fail closed.

Only a user clicking the local UI can invoke update_price. No credentials are
returned to the dashboard, logged, or stored outside the dedicated browser profile.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import re
import time
import traceback
from urllib.parse import urlsplit

from pricing import cents

PLATFORM = "https://xmeta.x-metash.cn/prod/xmeta_mall/index.html"
API = "https://api.x-metash.cn"
READ_PATHS = {"/h5/mySell/listAppNew", "/h5/mySell/sellArchiveGoodsList", "/h5/goods/archiveGoods", "/h5/mySell/priceLimits", "/h5/goods/details"}
WRITE_PATH = "/h5/mySell/updateAmount/confirm"


class PlatformConnectionError(ValueError):
    """Session/transport failures require stopping the whole batch."""
    stop_batch = True


class MarketCountError(ValueError):
    """One product's inconsistent market count cannot establish absence of rivals."""


class PlatformRejectionError(ValueError):
    """A documented business rejection says this price change was not accepted."""
    definitive_rejection = True


class InventoryPaginationError(ValueError):
    """Inventory moved between pages; discard this partial read and retry once."""


def identifier(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)) or str(value).strip() == "":
        raise ValueError("平台商品标识缺失，已停止本轮检查")
    return str(value)


def enum(value):
    if isinstance(value, bool):
        return -1
    try:
        number = int(value)
        return number if str(number) == str(value) else -1
    except (ValueError, TypeError):
        return -1


def parse_page(data, key):
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ValueError("平台列表格式发生变化，已停止检查")
    total = enum(data.get("total"))
    if total < 0 or any(not isinstance(row, dict) for row in data[key]):
        total_info = repr(data.get("total"))[:40] if data.get("total") is None or isinstance(data.get("total"), (int, float, bool)) else type(data.get("total")).__name__
        raise ValueError(f"平台分页信息无法确认，已停止检查（{key} 返回 {len(data[key])} 条，总数字段 {total_info}）")
    return data[key], total


def is_single_spot(offer):
    return (enum(offer.get("goodsType")) == 1 and enum(offer.get("dealType")) == 0
            and enum(offer.get("sellStatus")) == 1 and enum(offer.get("packageCount", 1)) in (0, 1))


def has_limit_boundary(value):
    """Distinguish unconfigured bounds from real values without truthiness coercion."""
    if value is None:
        return False
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("平台限价边界格式异常，未提交改价")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("平台限价边界格式异常，未提交改价") from None
    if not number.is_finite():
        raise ValueError("平台限价边界格式异常，未提交改价")
    return number != 0


def validate_limits(data, target):
    if not isinstance(data, dict):
        raise ValueError("无法确认平台价格限制，未提交改价")
    status = enum(data.get("priceStatus"))
    low, high = data.get("minGoodsPrice"), data.get("maxGoodsPrice")
    # Live priceLimits responses use 9 with null bounds when no limit is configured.
    # The official frontend applies checks only to 0/1/2. Accept the observed 9
    # shape explicitly; unexpected types or contradictory bounds still fail closed.
    if status == 9 and (has_limit_boundary(low) or has_limit_boundary(high)):
        raise ValueError("平台无限价标记与价格边界冲突，未提交改价")
    if status == 0 and (low or high):
        # The public frontend reverses these field names. Do not guess a bound.
        raise ValueError("平台双向价格限制需要核实，请在平台手动改价")
    if status == 1 and high and target > cents(high):
        raise ValueError("建议价超过平台最高价，未提交改价")
    if status == 2 and low and target < cents(low):
        raise ValueError("建议价低于平台最低价，未提交改价")
    if status not in (0, 1, 2, 9):
        raise ValueError("平台价格限制类型未识别，未提交改价")
    increase = enum(data.get("increaseStatus"))
    if increase == 9:
        if has_limit_boundary(data.get("minIncrease")) or has_limit_boundary(data.get("maxIncrease")):
            raise ValueError("平台无涨跌幅限制标记与边界冲突，未提交改价")
        return
    if data.get("beforeDealPrice"):
        previous = Decimal(str(data["beforeDealPrice"]))
        if increase == 0 and (data.get("minIncrease") or data.get("maxIncrease")):
            raise ValueError("平台日涨跌幅区间需要核实，请在平台手动改价")
        if increase not in (0, 1, 2):
            raise ValueError("平台日涨跌幅限制无法识别，未提交改价")
        for kind, field in [(1, "maxIncrease"), (2, "minIncrease")]:
            if increase == kind and data.get(field):
                rate = Decimal(str(data[field]))
                if not previous.is_finite() or not rate.is_finite():
                    raise ValueError("平台价格限制无效")
                bound = int((previous * (1 + rate / 100) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                if (kind == 1 and target > bound) or (kind == 2 and target < bound):
                    raise ValueError("建议价超出平台日涨跌幅限制，未提交改价")


class XmetaAdapter:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.context = None
        self.page = None
        self.playwright = None
        self.opened = False
        self.last_request = 0
        self.scan_started = 0
        self.scan_token = None
        self.account_scope = None

    def is_open(self):
        return self.opened

    def open_login(self):
        try:
            from playwright.sync_api import sync_playwright
            if self.context and self.page and not self.page.is_closed():
                self.page.bring_to_front()
                return
            self.data_dir.mkdir(exist_ok=True)
            if not self.playwright:
                self.playwright = sync_playwright().start()
            error = None
            # 专用登录窗口优先使用 Edge，启动失败时才回退到 Chrome。
            for channel in ("msedge", "chrome"):
                try:
                    self.context = self.playwright.chromium.launch_persistent_context(
                        str(self.data_dir / "browser-profile"), channel=channel,
                        # 页面使用真实窗口大小；只设置初始外框，不固定网页视口。
                        headless=False, no_viewport=True,
                        args=["--window-size=520,850"],
                        accept_downloads=False,
                    )
                    break
                except Exception as exc:
                    error = exc
            if not self.context:
                raise error or ValueError("无法打开浏览器")
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.context.on("close", lambda _: setattr(self, "opened", False))
            self.page.goto(PLATFORM + "#/pages/main/index", wait_until="domcontentloaded", timeout=45000)
            self.opened = True
        except Exception:
            self.opened = False
            raise ValueError("无法打开专用登录窗口。请确认已安装 Edge 或 Chrome，并关闭旧的专用窗口后重试。") from None

    def token(self):
        if not self.page or self.page.is_closed() or urlsplit(self.page.url).hostname != "xmeta.x-metash.cn":
            raise PlatformConnectionError("请在专用窗口打开 Xmeta 并完成登录")
        token = self.page.evaluate("() => localStorage.getItem('initToken')")
        if not isinstance(token, str) or not token or token in ("null", "undefined") or len(token) > 20000:
            raise PlatformConnectionError("尚未登录 Xmeta，请在专用窗口完成登录后重试")
        return token

    def post(self, path, data, *, write=False):
        if path not in READ_PATHS and not (path == WRITE_PATH and write):
            raise ValueError("不支持此平台操作")
        if (write or path == "/h5/mySell/priceLimits") and (not self.scan_token or not self.account_scope):
            raise PlatformConnectionError("当前连接尚未检查行情，请先读取我的出售或点击立即检查")
        token = self.token()
        if self.scan_token and token != self.scan_token:
            raise PlatformConnectionError("登录会话发生变化，请重新连接后检查")
        # Inventory enumeration precedes the first quote. Give that work its
        # own room inside the 360-second actor timeout; quote ages stay 240s.
        if self.scan_started and time.monotonic() - self.scan_started > 300:
            raise ValueError("本轮商品较多或网络较慢，未能及时完成检查")
        delay = .5 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        try:
            response = self.context.request.post(API + path, data=data,
                headers={"Authorization": "Bearer " + token, "Version": "2.2.31", "gzip": "true", "Origin": "https://xmeta.x-metash.cn", "Referer": PLATFORM},
                timeout=40000, max_redirects=0)
            if response.status in (401, 403):
                raise PlatformConnectionError("登录已过期或平台拒绝访问，请重新登录")
            if response.status == 429:
                raise PlatformConnectionError("平台请求频率受限，请延长检查间隔后重试")
            if response.status != 200:
                raise PlatformConnectionError(f"平台请求未完成（HTTP {response.status}），请稍后重试")
            try:
                result = response.json()
            except ValueError:
                raise PlatformConnectionError("平台返回格式异常，请稍后重试") from None
            if not isinstance(result, dict) or enum(result.get("code")) != 200:
                code = enum(result.get("code")) if isinstance(result, dict) else -1
                if code == 401:
                    raise PlatformConnectionError("登录已过期，请重新登录")
                message = result.get("msg") or result.get("message") if isinstance(result, dict) else None
                if isinstance(message, str):
                    message = message.replace(token, "[登录凭证已隐藏]")
                    message = re.sub(r"(?i)bearer\s+[^\s,;]+", "[登录凭证已隐藏]", message)
                    message = re.sub(r"[\x00-\x1f\x7f]", " ", message)
                    message = " ".join(message.split())[:240]
                else:
                    message = "请到平台查看提示"
                error_type = PlatformRejectionError if write and code in (40632, 55040) else ValueError
                error = error_type(f"平台未接受请求（状态 {code}）：{message}")
                if write and code == 55040:
                    error.platform_cooldown_seconds = 1800
                raise error
            return result.get("data")
        except ValueError:
            raise
        except Exception:
            raise PlatformConnectionError("平台请求超时或返回格式异常，请检查网络后重试") from None

    def pages(self, path, params, key, page_key, id_key, size=20):
        result, seen = [], {}
        expected_total = None
        for page in range(1, 51):
            data = self.post(path, {**params, page_key: page, "pageSize": size})
            # The payment-in-progress view returns {goods: [], total: null}
            # when empty. Do not relax complete on-sale inventory validation.
            if (path == "/h5/mySell/listAppNew" and params.get("goodsStatus") == 2 and page == 1
                and isinstance(data, dict) and data.get(key) == [] and data.get("total") is None):
                return []
            rows, total = parse_page(data, key)
            if expected_total is not None and total != expected_total:
                raise InventoryPaginationError("分页期间挂单数量变化，请重新检查")
            expected_total = total
            if not rows and len(result) != total:
                raise InventoryPaginationError("挂单分页不完整，已停止检查")
            for row in rows:
                if path == "/h5/mySell/listAppNew" and enum(row.get("businessType")) == 2:
                    row_id = f"group:{identifier(row.get('archiveId'))}:{cents(row.get('amount'))}"
                else:
                    row_id = identifier(row.get(id_key))
                if row_id in seen:
                    source = "我的出售" if path == "/h5/mySell/listAppNew" else "同价挂单"
                    previous = seen[row_id]
                    raise InventoryPaginationError(f"{source}第 {page} 页出现重复挂单，已停止检查（{str(row.get('name') or row_id)[:80]}；标识 {row_id}，首次第 {previous[0]} 页；本页 {len(rows)} 条，共 {total} 条）")
                seen[row_id] = (page, row)
                result.append(row)
            if len(result) == total:
                return result
            if len(result) > total:
                raise InventoryPaginationError("挂单数量无法确认，已停止检查")
        raise ValueError("挂单超过单轮读取上限，暂不能确认最低价")

    def market_quotes(self, params, own_ids):
        """Read an ascending-price prefix sufficient to establish the rival minimum.

        sortType=1 is the platform's lowest-price ordering. Every returned row
        must respect that order. Own inventory is still read completely.
        """
        offers, seen = [], set()
        previous_price, expected_total = None, None
        for page in range(1, 51):
            data = self.post("/h5/goods/archiveGoods", {**params, "sortType": 1, "page": page, "pageSize": 20})
            rows, total = parse_page(data, "goodsArchiveList")
            if expected_total is not None and total != expected_total:
                raise MarketCountError("该款行情数量在分页期间发生变化，请稍后重新检查")
            expected_total = total
            for offer in rows:
                item_id = identifier(offer.get("goodsId"))
                if item_id in seen:
                    raise ValueError("最低价挂单分页出现重复，请重新检查")
                if any(key not in offer for key in ("goodsType", "dealType", "sellStatus")):
                    raise ValueError("市场挂单类型字段缺失，无法可靠比价")
                price = cents(offer.get("goodsPrice"))
                if previous_price is not None and price < previous_price:
                    raise ValueError("平台未按价格升序返回挂单，暂不能确认最低价")
                previous_price = price
                seen.add(item_id)
                offers.append(offer)
            count_consistent = len(offers) <= total
            complete = count_consistent and len(offers) == total
            # The official frontend appends goodsArchiveList independently of total.
            # A live first page contained 17 rows with total=16. The fully validated
            # ascending prefix still establishes the rival minimum, but cannot
            # establish that every market listing has been read.
            if any(is_single_spot(x) and identifier(x.get("goodsId")) not in own_ids for x in offers):
                return offers, complete
            if not count_consistent or (not rows and not complete):
                raise MarketCountError(f"该款行情数量不一致，暂不能确认最低价，请稍后检查（第 {page} 页，本页 {len(rows)} 条，累计 {len(offers)} 条，平台总数 {total}）")
            if complete:
                return offers, True
        raise ValueError("最低价范围内没有找到可比较挂单，请稍后重试")

    def inventory(self):
        for attempt in range(2):
            try:
                return self._inventory(page_size=100 if attempt == 0 else 200)
            except InventoryPaginationError:
                if attempt:
                    raise
                time.sleep(.5)

    def _inventory(self, page_size=100):
        # A larger page reduces requests and cross-page movement in the live
        # inventory. Totals, duplicate checks and the final fingerprint remain.
        cards = self.pages("/h5/mySell/listAppNew", {"platformId": None, "orderVal": "", "goodsStatus": 1}, "goods", "pageNum", "id", page_size)
        rows = []
        for card in cards:
            if enum(card.get("businessType")) == 2:
                count = enum(card.get("count"))
                if count < 0:
                    raise ValueError("聚合商品数量字段异常，已停止检查")
                expanded = self.pages("/h5/mySell/sellArchiveGoodsList", {"archiveId": card["archiveId"], "amount": card["amount"], "orderVal": "", "goodsStatus": 1}, "goods", "pageNum", "id", page_size)
                if count != len(expanded):
                    raise InventoryPaginationError("聚合商品数量发生变化，请重新检查")
                rows.extend(expanded)
            else:
                rows.append(card)
        ids = [identifier(row.get("id")) for row in rows]
        if len(ids) != len(set(ids)):
            raise InventoryPaginationError("自己的挂单存在重复，暂不能可靠排除")
        return rows

    @staticmethod
    def own_fingerprint(rows):
        return sorted((identifier(x.get("id")), cents(x.get("amount")), *(str(x.get(key)) for key in ("archiveId", "platformId", "goodsType", "dealType", "packageCount", "goodsStatus", "sellStatus", "status"))) for x in rows)

    @staticmethod
    def normalize_inventory_item(row):
        item_id = identifier(row.get("id"))
        archive = str(row.get("archiveId") or "")
        platform = str(row.get("platformId") or "")
        item = {"id": item_id, "archiveId": archive, "platformId": platform,
                "name": str(row.get("name") or "未命名商品"), "serial": str(row.get("collectionNumber") or item_id),
                "price": cents(row.get("amount")), "rival": None, "saleStatus": "on_sale", "marketComplete": False,
                "checkedAt": 0, "goodsType": enum(row.get("goodsType")), "dealType": enum(row.get("dealType")),
                "packageCount": row.get("packageCount", 1)}
        if not archive or not platform:
            item["error"] = "商品尚未归档或平台标识缺失，已跳过"
        elif enum(row.get("goodsType")) != 1 or enum(row.get("dealType")) != 0 or enum(row.get("packageCount", 1)) not in (0, 1):
            item["error"] = "仅支持普通单件现货；打包、预售或未识别类型已跳过"
        return item

    def reconcile_inventory(self, own, latest_own, items, markets):
        """Invalidate changed product groups while preserving unrelated quotes.

        Both inventories must have passed the normal complete-pagination checks.
        New/removed/moved listings affect their old and new archive/platform
        groups, since any of them may change exclusion of the seller's offers.
        """
        previous_fingerprints = {row[0]: row for row in self.own_fingerprint(own)}
        latest_fingerprints = {row[0]: row for row in self.own_fingerprint(latest_own)}
        changed_ids = {item_id for item_id in previous_fingerprints.keys() | latest_fingerprints.keys()
                       if previous_fingerprints.get(item_id) != latest_fingerprints.get(item_id)}
        affected = {(str(row.get("archiveId") or ""), str(row.get("platformId") or ""))
                    for row in own + latest_own if identifier(row.get("id")) in changed_ids}
        # A newly owned unarchived/moved listing can already appear in another
        # group's quotes. Reconcile actual quoted IDs as well as inventory group
        # fields so it can never remain incorrectly classified as a rival.
        for group, offers in markets.items():
            if any(identifier(offer.get("goodsId")) in changed_ids for offer in offers):
                affected.add(group)
        previous_items = {item["id"]: item for item in items}
        result = []
        for row in latest_own:
            item = self.normalize_inventory_item(row)
            group = (item["archiveId"], item["platformId"])
            if group in affected:
                item["error"] = "该款在售挂单在检查期间发生变化，请刷新后再跟价"
            else:
                previous = previous_items[item["id"]]
                for key in ("rival", "marketMinimum", "marketComplete", "checkedAt", "error"):
                    if key in previous:
                        item[key] = previous[key]
            result.append(item)
        return result

    def read_items(self):
        self.scan_token = self.token()
        self.scan_started = time.monotonic()
        try:
            own = self.inventory()
            if own:
                detail = self.post("/h5/goods/details", {"goodsId": own[0]["id"]})
                if not isinstance(detail, dict):
                    raise ValueError("无法确认当前卖家，已停止检查")
                seller = identifier(detail.get("uid"))
                self.account_scope = hashlib.sha256(seller.encode()).hexdigest()[:24]
            else:
                self.account_scope = None
            own_ids = {identifier(x.get("id")) for x in own}
            markets = {}
            market_complete = {}
            market_times = {}
            market_errors = {}
            items = []
            for row in own:
                item = self.normalize_inventory_item(row)
                archive, platform = item["archiveId"], item["platformId"]
                items.append(item)
                if item.get("error"):
                    continue
                group = (archive, platform)
                if group in market_errors:
                    item["error"] = market_errors[group]
                    continue
                if group not in markets:
                    market_times[group] = time.time()
                    params = {"archiveId": row["archiveId"], "platformId": row["platformId"], "sellStatus": 1,
                              "dealType": 0, "goodsType": "", "sortType": 1, "isPayBond": "", "startTime": "", "endTime": "", "fancyNumberType": "", "maxPrize": None, "minPrize": None}
                    try:
                        markets[group], market_complete[group] = self.market_quotes(params, own_ids)
                    except MarketCountError as exc:
                        market_errors[group] = item["error"] = str(exc)
                        continue
                market = markets[group]
                by_id = {identifier(x.get("goodsId")): x for x in market}
                last_price = cents(market[-1].get("goodsPrice")) if market else None
                group_consistent = True
                for mine in own:
                    if (str(mine.get("archiveId") or ""), str(mine.get("platformId") or "")) != group:
                        continue
                    if enum(mine.get("goodsType")) != 1 or enum(mine.get("dealType")) != 0 or enum(mine.get("packageCount", 1)) not in (0, 1):
                        continue
                    mine_price = cents(mine.get("amount"))
                    quote = by_id.get(identifier(mine.get("id")))
                    if quote is not None:
                        if not is_single_spot(quote) or cents(quote.get("goodsPrice")) != mine_price:
                            group_consistent = False
                            break
                    elif market_complete[group] or last_price is None or mine_price < last_price:
                        # A missing own row below the returned prefix would violate
                        # the ordering/list consistency; don't use it to set a floor.
                        group_consistent = False
                        break
                if not group_consistent:
                    item["error"] = "市场列表与我的出售不一致，请稍后重新检查"
                    continue
                candidates = []
                comparable_prices = []
                for offer in market:
                    if "archiveId" in offer and identifier(offer["archiveId"]) != archive:
                        raise ValueError("市场返回了不同藏品，停止比价")
                    if "platformId" in offer and identifier(offer["platformId"]) != platform:
                        raise ValueError("市场返回了不同平台商品，停止比价")
                    if any(key not in offer for key in ("goodsType", "dealType", "sellStatus")):
                        item["error"] = "市场挂单类型字段缺失，无法可靠比价"
                        break
                    if enum(offer["goodsType"]) != 1 or enum(offer["dealType"]) != 0 or enum(offer["sellStatus"]) != 1:
                        continue
                    if enum(offer.get("packageCount", 1)) not in (0, 1):
                        continue
                    offer_price = cents(offer.get("goodsPrice"))
                    comparable_prices.append(offer_price)
                    if identifier(offer.get("goodsId")) not in own_ids:
                        candidates.append(offer_price)
                if not item.get("error"):
                    item["rival"] = min(candidates) if candidates else None
                    item["marketMinimum"] = min(comparable_prices)
                    item["marketComplete"] = True
                    item["checkedAt"] = market_times[group]
            # Detect newly added/removed own listings that could corrupt self exclusion.
            latest_own = self.inventory()
            return self.reconcile_inventory(own, latest_own, items, markets)
        except ValueError:
            raise
        except Exception as exc:
            # Report a code location only, never exception text, tokens or payloads.
            location = traceback.extract_tb(exc.__traceback__)[-1]
            raise ValueError(f"行情读取程序异常（{type(exc).__name__}:{location.lineno}），请反馈此错误编号") from None
        finally:
            self.scan_started = 0

    def prepare_update(self, item, target):
        if type(target) is not int or target < 100 or target >= item["price"]:
            raise ValueError("价格必须至少 1 元，且只允许向下跟价")
        if target * 5 < item["price"]:
            raise PlatformRejectionError("平台单次改价不得低于原价的 20%，请按最新建议分次跟价")
        limits = self.post("/h5/mySell/priceLimits", {"archiveId": item["archiveId"]})
        validate_limits(limits, target)

    def update_price(self, item, target):
        result = self.post(WRITE_PATH, {"goodsId": item["id"], "amount": format(Decimal(target) / 100, ".2f")}, write=True)
        if isinstance(result, dict) and result.get("isPay") not in (None, False, 0, "0", "false"):
            raise ValueError("平台要求补保证金，请在 Xmeta 官方页面处理")

    def confirm_update(self, item, target):
        """Confirm one listing with one read, without rescanning inventory/quotes.

        The official goods detail page reads id/uid/archiveId/platformId and
        amount directly from goodsGoodsDetail's response. Do not treat an
        update acknowledgement alone as proof that the new price took effect.
        Any failure leaves the caller responsible for retaining its pending lock.
        """
        if type(target) is not int or target < 100:
            raise ValueError("待确认售价无效")
        expected_scope = self.account_scope
        if not expected_scope:
            raise PlatformConnectionError("当前卖家身份未确认，请重新读取我的出售")
        detail = self.post("/h5/goods/details", {"goodsId": item["id"]})
        if not isinstance(detail, dict):
            raise ValueError("改价后商品详情格式异常，请到 Xmeta 核实结果")
        try:
            identity_matches = all(identifier(detail.get(key)) == identifier(item.get(key))
                                   for key in ("id", "archiveId", "platformId"))
            seller_scope = hashlib.sha256(identifier(detail.get("uid")).encode()).hexdigest()[:24]
        except ValueError:
            raise ValueError("改价后挂单身份无法确认，请到 Xmeta 核实结果") from None
        if seller_scope != expected_scope or self.account_scope != expected_scope:
            raise PlatformConnectionError("改价后卖家身份不一致，请重新连接账号并核实结果")
        if not identity_matches:
            raise ValueError("改价后商品身份不一致，请到 Xmeta 核实结果")
        if not is_single_spot(detail):
            raise ValueError("改价后挂单已不在售或类型发生变化，请到 Xmeta 核实结果")
        if cents(detail.get("amount")) != target:
            raise ValueError("改价后未读到预期售价，请到 Xmeta 核实结果")

    def close(self):
        context, playwright = self.context, self.playwright
        self.context = self.page = self.playwright = None
        self.opened = False
        self.scan_token = None
        self.scan_started = 0
        try:
            if context:
                context.close()
        except Exception:
            pass  # A manually closed browser must not prevent service shutdown.
        finally:
            if playwright:
                try:
                    playwright.stop()
                except Exception:
                    pass
