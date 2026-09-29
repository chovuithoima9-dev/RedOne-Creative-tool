"""Translate raw API / network errors into clear, user-facing Vietnamese.

The generator surfaces a lot of low-level error strings to the gallery
(Google Flow JSON blobs, reCAPTCHA / 429 throttle bodies, Shakker/Liblib
responses, …). End users can't act on
`{"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}`.

`friendly_error()` (Google Flow) and `friendly_shakker_error()` (Shakker)
map the most common failure signatures to a short, plain-Vietnamese message
that says WHAT happened, whether it's TEMPORARY or PERMANENT, and the exact
NEXT STEP. The original raw string is preserved by the caller (broadcast as
`error_detail`, kept in app.log) so debugging info is never lost — this only
changes what's shown on the card.

Ordering matters: buckets are checked most-specific → most-generic and the
FIRST match wins. In particular the transient reCAPTCHA/throttle bucket is
checked BEFORE the daily-quota bucket (both contain "429"), and the
prominent-people filter BEFORE the generic safety bucket, so the user gets
the precise cause + remedy instead of a vague one.

Note: the tool runs Flow through the Chrome-extension bridge only (Vertex
AI + Playwright/Cloak modes were removed), so Flow remedies point at the
Tài Khoản tab / Chrome labs.google session — never at Vertex.
"""
from __future__ import annotations


def friendly_error(raw: str) -> str:
    """Return a clear Vietnamese message for a raw Google Flow error string.

    Falls back to the trimmed raw string when nothing matches, so unusual
    errors are still shown rather than swallowed.
    """
    if not raw:
        return "Lỗi không xác định."
    s = str(raw)
    low = s.lower()

    def has(*subs: str) -> bool:
        return any(sub in low for sub in subs)

    # ── reCAPTCHA / bot-detection / transient rate throttle (TEMPORARY) ──
    # CHECK BEFORE the daily-quota bucket. Google's transient throttle is a
    # 429 RESOURCE_EXHAUSTED whose reason is
    # PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC + "reCAPTCHA evaluation
    # failed" — so it ALSO contains "429"/"resource_exhausted". It must NOT be
    # labeled "hết quota hôm nay": it's temporary and the tool auto-retries.
    if has("recaptcha", "captcha", "unusual_activity", "unusual activity",
           "unusual traffic", "too_much_traffic", "too much traffic",
           "user_requests_throttled", "throttled", "throttle",
           "rate_limit", "rate limit", "[7]", "error: [7]"):
        return (
            "Google tạm nghẽn vì gửi yêu cầu quá nhanh liên tục trong thời gian ngắn (Rate Limit / Throttled). "
            "Đây là lỗi TẠM THỜI (không phải hết quota ngày). Tool sẽ tự giãn cách, "
            "hoặc bạn chỉ cần bấm 'Gen lại' sau vài giây là sẽ thành công."
        )

    # ── Daily quota exhausted (free Labs Flow per-model daily cap) ──
    # Reached only when the 429 is NOT the transient throttle above — i.e. a
    # genuine per-model daily limit. Resets the next day.
    if has("per_model_daily_quota", "daily", "quota", "quota_reached",
           "resource has been exhausted"):
        return (
            "Tài khoản này đã hết lượt tạo miễn phí trong hôm nay (Google "
            "giới hạn quota theo ngày). Đổi sang tài khoản Google khác ở tab "
            "Tài Khoản, hoặc chờ sang ngày mai quota tự reset."
        )

    # ── Prominent-people / public-figure filter (PERMANENT for this prompt) ──
    # Google refuses to generate recognizable real public figures. Retrying
    # the same prompt/ref image will always fail.
    if has("prominent_people", "prominent people", "prominent person",
           "people_filter", "public figure", "celebrity", "celebrities"):
        return (
            "Google chặn vì prompt hoặc ảnh tham chiếu có người nổi tiếng "
            "(người thật của công chúng — nghệ sĩ, ca sĩ, chính trị gia…). "
            "Google không cho tạo ảnh/video mô phỏng người nổi tiếng. Bỏ "
            "tên/ảnh người nổi tiếng hoặc dùng nhân vật hư cấu rồi gen lại — "
            "gen lại y nguyên sẽ vẫn lỗi."
        )

    # ── IP / copyright block on the INPUT (reference) image (PERMANENT) ──
    # PUBLIC_ERROR_IP_INPUT_IMAGE / _OUTPUT_IMAGE — Google refuses because the
    # REFERENCE image (or result) carries copyrighted / IP-protected content: a
    # recognizable character, brand/logo, artwork, or a real public figure.
    # Retrying the same image always fails — the user must swap the reference. ──
    if has("ip_input_image", "ip_output_image", "public_error_ip_"):
        return (
            "Google chặn vì ẢNH THAM CHIẾU vi phạm bản quyền / sở hữu trí tuệ — "
            "thường do ảnh có nhân vật, logo/thương hiệu, tác phẩm có bản quyền, "
            "hoặc người nổi tiếng thật. Đổi sang ảnh tham chiếu khác (không chứa "
            "nội dung có bản quyền / người thật) rồi gen lại — gen lại y nguyên "
            "sẽ vẫn lỗi."
        )

    # ── Safety / content-policy block (PERMANENT for this prompt) ──
    if has("safety", "blocked", "prohibited", "violat", "responsible ai",
           "content policy", "finish_reason", "image_safety", "person/face",
           "sensitive"):
        return (
            "Nội dung bị Google chặn vì vi phạm chính sách (nhạy cảm, bạo "
            "lực, bản quyền, người thật…). Sửa lại prompt cho an toàn hơn rồi "
            "gen lại — gen lại y nguyên sẽ vẫn lỗi."
        )

    # ── Session / login expired (bridge: Chrome's labs.google session died) ──
    if has("session", "hết hạn", "expired", "session dead", "unauthorized",
           "401", "đăng nhập", "login lại"):
        return (
            "Phiên đăng nhập Google của tài khoản này đã hết hạn. Mở Chrome "
            "và đăng nhập lại labs.google bằng tài khoản đó (extension sẽ tự "
            "đồng bộ), hoặc đổi sang tài khoản khác ở tab Tài Khoản, rồi gen lại."
        )

    # ── Permission / access denied (403, non-reCAPTCHA) ──
    # reCAPTCHA-flavored 403s are already handled above; this catches a plain
    # permission/403 denial — usually a broken session or a restricted account.
    if has("permission_denied", "permission denied", "403",
           "does not have permission", "caller does not have"):
        return (
            "Google từ chối quyền truy cập (403). Thường do phiên đăng nhập "
            "lỗi hoặc tài khoản bị hạn chế — đăng nhập lại, hoặc đổi sang tài "
            "khoản Google khác ở tab Tài Khoản."
        )

    # ── Model not found / unsupported ──
    if has("not_found", "not found", "404", "không hỗ trợ", "unsupported",
           "is not supported", "no such model", "model not"):
        return (
            "Model bạn chọn không khả dụng hoặc Google không còn hỗ trợ. Chọn "
            "model khác trong tab tạo rồi gen lại."
        )

    # ── Timeout — also Google's PUBLIC_ERROR_VIDEO_GENERATION_TIMED_OUT, whose
    # raw spells it "timed_out" (underscore), NOT caught by "timed out". ──
    if has("timeout", "timed out", "timed_out", "deadline", "deadline_exceeded",
           "etimedout", "[4]", "error: [4]"):
        return (
            "Google xử lý quá lâu nên hết thời gian chờ (máy chủ Google đang "
            "quá tải hoặc nội dung phức tạp). Lỗi TẠM THỜI phía Google — bấm "
            "'Gen lại' để thử lại."
        )

    # ── Network / connectivity ──
    if has("failed to fetch", "connection", "connect", "network",
           "getaddrinfo", "econn", "name resolution", "ssl", "max retries",
           "temporary failure", "unreachable"):
        return (
            "Không kết nối được tới Google. Kiểm tra Internet/VPN rồi bấm "
            "'Gen lại'."
        )

    # ── Download / save failure ──
    if has("download failed", "download", "tải", "save", "ghi file",
           "no media_id", "media_id"):
        return (
            "Đã tạo xong nhưng tải/lưu file kết quả thất bại (mạng gián đoạn "
            "hoặc lỗi ghi ổ đĩa). Bấm 'Gen lại' để thử lại."
        )

    # ── Transient Google server errors — also bare INTERNAL /
    # PUBLIC_ERROR_INTERNAL, not caught by "internal error". Checked near the end
    # so the more specific buckets above win first. ──
    if has("internal error", "internal_error", "internal", "500", "503", "502",
           "unavailable", "try again", "backend error", "service is currently",
           "[13]", "error: [13]", "[14]", "error: [14]"):
        return (
            "Google gặp lỗi nội bộ ở phía máy chủ (Mã 13: máy chủ Google quá tải hoặc lỗi xử lý tạm thời). "
            "Lỗi TẠM THỜI — chờ vài giây rồi bấm 'Gen lại', hoặc giảm bớt ảnh tham chiếu/số luồng gen."
        )

    # ── Unsafe generation — RAI/content-safety block (PERMANENT for this
    # prompt+image). The raw is INVALID_ARGUMENT + reason
    # PUBLIC_ERROR_UNSAFE_GENERATION. For Imagen image-to-image (R2I) the usual
    # cause is a real person/face in the REFERENCE image. MUST be checked before
    # the generic INVALID_ARGUMENT bucket below (the raw also carries it). ──
    if has("unsafe_generation", "public_error_unsafe", "unsafe"):
        return (
            "Google chặn vì nội dung không an toàn (chính sách RAI). Lỗi do "
            "PROMPT hoặc ẢNH THAM CHIẾU bị gắn cờ. Với 'tạo ảnh từ ảnh' bằng "
            "Imagen, thường là do ảnh tham chiếu có người/khuôn mặt thật — "
            "Imagen không cho tái tạo người thật. Cách xử lý: sửa prompt an "
            "toàn hơn, đổi ảnh tham chiếu không có người thật, hoặc dùng model "
            "NANO BANANA (thoáng hơn cho nhân vật). Gen lại y nguyên sẽ vẫn lỗi."
        )

    # ── Request rejected as invalid, no specific reason matched above —
    # usually an unsupported prompt/parameter combo for the chosen model. ──
    if has("invalid_argument", "invalid argument"):
        return (
            "Google từ chối yêu cầu vì tham số không hợp lệ — thường do prompt "
            "hoặc tổ hợp tuỳ chọn không hợp với model đang chọn. Thử đổi model, "
            "bỏ ảnh tham chiếu, hoặc sửa prompt rồi gen lại."
        )

    # Fallback: show the raw error (trimmed) so nothing is hidden.
    trimmed = s.strip()
    if len(trimmed) > 300:
        trimmed = trimmed[:300] + "…"
    return trimmed or "Lỗi không xác định."


def friendly_shakker_error(raw: str) -> str:
    """Shakker-specific error mapping.

    Kept separate from `friendly_error()` because Shakker (Liblib AI) has
    its own vocabulary — "power" instead of credits, possible Chinese
    error strings from the backend — and its remedies differ (re-login at
    shakker.ai, top up power, switch shakker account). Used by
    routers/shakker.py and services/shakker_client.py.
    """
    if not raw:
        return "Lỗi không xác định."
    s = str(raw)
    low = s.lower()

    def has(*subs: str) -> bool:
        return any(sub in low for sub in subs)

    # ── Insufficient power / credits ──
    # Shakker calls credits "power" (Chinese backend: 积分 / 算力 / 余额不足).
    if has("insufficient", "not enough", "power not", "余额不足", "积分不足",
           "算力不足", "power 不足", "no power", "hết power", "không đủ power",
           "lack of power", "insufficient power"):
        return (
            "Tài khoản Shakker đã hết power (tín dụng để tạo ảnh). Nạp thêm "
            "power tại shakker.ai, hoặc đổi sang tài khoản Shakker khác trong "
            "Cài đặt."
        )

    # ── Token / session expired ──
    if has("phiên shakker", "token", "hết hạn", "expired", "401", "403",
           "unauthorized", "未登录", "登录", "登陆", "请登录", "not logged"):
        return (
            "Phiên đăng nhập Shakker đã hết hạn. Mở tab shakker.ai trong "
            "Chrome và đăng nhập lại — extension sẽ tự đồng bộ token mới — "
            "rồi gen lại."
        )

    # ── Safety / NSFW / illegal content (PERMANENT for this prompt) ──
    if has("nsfw", "illegal", "sensitive", "敏感", "违规", "违法", "safety",
           "blocked", "content policy", "色情", "审核"):
        return (
            "Ảnh bị Shakker chặn vì nội dung nhạy cảm / vi phạm chính sách. "
            "Sửa lại prompt rồi gen lại — gen lại y nguyên sẽ vẫn lỗi."
        )

    # ── Rate limit / too frequent (TEMPORARY) ──
    # NOTE: must NOT use the bare substring "rate" — the word "generate"
    # contains it, so any "...generate..." error would false-match here.
    if has("rate limit", "ratelimit", "rate-limit", "too many", "too frequent",
           "频繁", "429", "请求过于", "limit exceeded"):
        return (
            "Shakker đang giới hạn tốc độ (gửi yêu cầu quá nhanh). Đây là lỗi "
            "tạm thời — chờ vài giây rồi bấm 'Gen lại'."
        )

    # ── Model / LoRA unavailable ──
    if has("not found", "404", "model not", "lora", "已下架", "不存在",
           "removed", "unavailable model"):
        return (
            "Model hoặc LoRA đã chọn không còn khả dụng (có thể tác giả đã gỡ "
            "khỏi Shakker). Chọn model/LoRA khác rồi gen lại."
        )

    # ── Timeout ──
    if has("timeout", "timed out", "hết thời gian", "deadline", "超时"):
        return (
            "Quá thời gian chờ Shakker phản hồi (server có thể đang đông). "
            "Bấm 'Gen lại' để thử lại."
        )

    # ── Network ──
    if has("failed to fetch", "connection", "connect", "network", "econn",
           "getaddrinfo", "ssl", "unreachable", "name resolution",
           "không phải json", "lỗi mạng"):
        return (
            "Không kết nối được tới Shakker. Kiểm tra Internet/VPN rồi bấm "
            "'Gen lại'."
        )

    # ── Server error ──
    if has("máy chủ", "500", "502", "503", "internal", "服务器", "unavailable",
           "server error"):
        return (
            "Shakker đang lỗi tạm thời ở phía máy chủ. Chờ vài giây rồi bấm "
            "'Gen lại'."
        )

    # ── Generic generation failure — Shakker code 1020003 "Fail to generate the
    # image due to unexpected reasons". Their catch-all when the pipeline fails
    # for an unspecified reason: usually transient server-side, sometimes a
    # prompt/model/LoRA combo. Retryable. ──
    if has("1020003", "unexpected reason", "fail to generate", "failed to generate"):
        return (
            "Shakker tạo ảnh thất bại (lỗi phía máy chủ Shakker, thường tạm "
            "thời). Bấm 'Gen lại' để thử lại. Nếu cùng prompt cứ lỗi lặp lại, "
            "thử đổi/sửa prompt, đổi model hoặc LoRA."
        )

    # ── Download failed ──
    if has("tải ảnh", "download", "tải", "save"):
        return (
            "Đã tạo xong nhưng tải ảnh kết quả thất bại. Bấm 'Gen lại' để "
            "thử lại."
        )

    # Fallback — trimmed raw.
    trimmed = s.strip()
    if len(trimmed) > 300:
        trimmed = trimmed[:300] + "…"
    return trimmed or "Lỗi không xác định."
