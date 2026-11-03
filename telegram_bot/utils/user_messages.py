"""Chinese presentation for internal codes; never echo unknown exception text."""

STATES = {
    'prepared': '准备中', 'submitting': '正在提交', 'submitted': '已提交',
    'purchasing': '正在购买', 'exporting': '正在导出', 'collecting': '正在收集结果',
    'success': '成功', 'failed': '失败', 'unknown': '结果未知',
    'pending_review': '等待人工核实', 'manual': '需要人工处理',
    'unavailable': '暂不可用', 'timeout': '超时', 'format_changed': '回复格式已变化',
    'rate_limited': '请求受限', 'skipped': '已跳过', 'online': '在线',
    'offline': '离线', 'unauthorized': '尚未授权', 'error': '连接异常',
    'delivery_unknown': '交付结果待核实',
}

REASONS = {
    'shutting_down': '程序正在关闭，请稍后再试。',
    'empty_query': '查询内容不能为空。',
    'cache_hit': '已使用有效缓存，无需重复查询。',
    'query_completed': '查询已完成。',
    'existing_task_no_resubmission': '已存在相同任务，没有重复提交。',
    'no_available_balance': '没有符合条件的账号，可能被未结案任务或额度限制阻止，并非一定积分不足。',
    'no_eligible_account': '暂时没有符合接单条件的账号，请检查账号状态、未结案任务和额度。',
    'no_accounts_loaded': '尚未添加账号，请先在私聊中登录。',
    'account_not_online': '账号当前不在线。',
    'daily_query_limit_reached': '今日查询次数和预留名额已达到上限。',
    'account_has_unresolved_task': '账号有尚未结案的查询任务，请先核实旧任务。',
    'account_busy_or_daily_limit': '账号已有未结案任务，或今日查询额度已用完。',
    'account_unavailable_before_submission': '提交前发现账号不可用，没有发送查询。',
    'account_unavailable': '账号暂不可用，请检查连接或授权状态。',
    'telegram_flood_wait_no_submission': '电报要求冷却等待，没有发送新的查询。',
    'telegram_flood_wait': '电报请求受限，请等待冷却结束。',
    'query_reply_timeout': '查询已提交，但等待回复超时，请先核实目标记录。',
    'explicit_target_failure': '目标机器人明确返回查询失败或积分不足。',
    'unrecognized_target_reply': '目标回复格式无法确认，请人工核实。',
    'purchase_reply_timeout': '购买后的回复超时，结果不确定；系统不会再次购买。',
    'purchase_not_confirmed': '尚未确认购买结果，系统不会再次购买。',
    'verification_after_purchase': '购买后出现验证，请人工处理并核实费用。',
    'verification_during_pagination': '翻页途中出现验证，已停止收集并转人工。',
    'security_verification_requires_human': '目标要求图片文字验证，请人工处理。',
    'document_download_failed': '结果文件下载失败，尚未交付。',
    'empty_result': '没有收集到可交付的结果，请人工核实。',
    'delivery_requires_review': '查询结果已保存，但是否送达尚需核实；系统不会自动重发。',
    'result_saved_no_automatic_resend': '结果已保存，为避免重复交付，不会自动重发。',
    'error_before_submission': '提交前发生异常，没有发送查询。',
    'restart_requires_review': '程序重启后发现未结案任务，请核实目标记录；不会自动重做。',
    'restart_before_submission': '程序在提交前重启，任务已结束。',
    'restart_after_intent': '操作发起后程序重启，执行结果需要人工核实。',
    'cancelled_before_submission': '提交前已取消，没有发送查询。',
    'cancelled_after_intent': '操作发起后被取消，请核实实际结果。',
    'balance_query_timeout': '等待积分回复超时。',
    'balance_query_format_changed': '积分回复格式已变化，暂时无法解析。',
    'balance_query_failed': '积分查询失败，请稍后检查连接。',
    'balance_query_unavailable': '账号不可用，无法查询积分。',
    'balance_button_missing': '没有找到积分查询按钮。',
    'balance_pattern_not_matched': '未能从目标回复中解析积分。',
    'balance_reply_timeout': '等待积分回复超时。',
    'balance_transport_timeout': '积分查询连接超时。',
    'balance_error': '积分查询发生异常。',
    'minimum_24h_interval': '距离上次签到尝试不足二十四小时，已跳过。',
    'confirmed_by_reply': '已收到目标机器人确认成功的回复。',
    'checkin_button_missing': '没有找到签到按钮。',
    'checkin_reply_timeout': '等待签到确认超时，请核实实际签到结果。',
    'checkin_transport_timeout': '签到通信超时，请核实实际签到结果。',
    'checkin_error': '签到过程发生异常，请人工核实。',
    'cached_file_missing_or_outside_root': '缓存文件丢失或路径异常，需要人工检查。',
    'invalid_cache_kind': '缓存结果类型异常，需要人工检查。',
    'missing_image_or_candidates': '验证图片或候选按钮缺失，未点击。',
    'image_download_failed': '验证图片下载失败，未点击。',
    'low_or_unknown_confidence': '图片识别置信度不足或无法确认，未点击。',
    'invalid_target_index': '无法确认图片要求选择的位置，未点击。',
    'non_unique_exact_match': '识别文字无法唯一匹配候选按钮，未点击。',
    'repeated_resubmission_blocked': '已达到重新提交限制，不会重复发送查询。',
    'resubmission_reply_timeout': '验证通过后的查询回复超时，请人工核实。',
    'interaction_reply_timeout': '点击验证按钮后等待回复超时，请人工核实。',
    'unrecognized_interaction_reply': '无法确认验证后的回复含义，请人工核实。',
    'attempts_exhausted': '已达到图片识别尝试上限，停止自动处理。',
    'LowConfidence': '图片识别置信度不足，未点击验证按钮。',
    'manual_choice_timeout': '等待人工选择超时，请核实目标机器人记录。',
    'manual_prompt_changed': '原验证图片或选项已变化，未点击。',
    'manual_verification_unconfirmed': '未收到明确的验证通过确认，请人工核实。',
    'manual_query_abandoned': '管理员已结束任务，原查询未再次提交。',
    'RecognitionDisagreement': '图片的两种识别结果不一致，未点击验证按钮。',
    'InvalidConfidence': '图片识别未返回有效置信度，未点击验证按钮。',
    'InvalidProbabilities': '图片识别返回的数据异常，未点击验证按钮。',
    'EmptyRecognition': '图片中未识别出有效文字，未点击。',
    'OCRBusyOrClosed': '图片识别服务忙碌或已关闭，未点击。',
    'InstructionIndexAmbiguous': '无法确定图片要求选择第几个字，未点击。',
    'InvalidImageSize': '验证图片尺寸异常，未点击。',
    'InvalidCandidates': '候选按钮无效，未点击。',
    'TimeoutError': '操作超时，需要核实实际结果。',
    'ConnectionError': '网络连接失败。', 'NetworkError': '网络通信异常。',
    'FloodWaitError': '电报要求冷却等待。',
    'AuthKeyUnregisteredError': '登录授权已失效，需要重新核实账号授权。',
    'SessionRevokedError': '账号登录授权已被撤销。',
    'RuntimeError': '运行过程发生异常，具体原因需查看维护日志。',
    'ValueError': '回复格式或数据校验异常。',
    'OSError': '文件或网络操作失败。',
    'FileNotFoundError': '所需文件不存在。',
    'PermissionError': '文件访问权限不足。',
}

STAGES = {
    'candidates': '提取验证按钮', 'image_download': '下载验证图片',
    'ocr': '识别图片文字', 'candidate_match': '匹配候选按钮',
    'button_click': '点击验证按钮', 'wait_after_click': '等待验证回复',
    'resubmit': '验证通过后提交查询', 'wait_result': '等待查询结果',
}


def state_text(value) -> str:
    return STATES.get(str(value), '状态未知')


def reason_text(value) -> str:
    code = str(value or '')
    if not code:
        return '暂无补充说明。'
    if code in REASONS:
        return REASONS[code]
    if code.startswith('verification_failed:'):
        return '验证未完成：' + reason_text(code.partition(':')[2])
    stage, sep, detail = code.partition(':')
    if sep and stage in STAGES:
        return STAGES[stage] + '时出现问题：' + reason_text(detail)
    if code.startswith('uncertain_'):
        return '执行结果尚不确定，请先人工核实。' + reason_text(code[len('uncertain_'):])
    if code.startswith('operator_review_'):
        return '该任务已人工核实并结案，没有自动重做。'
    # Unknown strings can include exception payloads or credentials. Never echo them.
    return '发生未识别的异常，请联系维护人员查看日志；系统不会自动重做。'
