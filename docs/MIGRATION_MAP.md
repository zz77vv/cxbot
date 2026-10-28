# 旧版逐函数迁移对照表

对照 legacy_inventory.json 的 105 个顶层定义；自动测试验证完整性与目标文件存在。映射表示职责接替；仅 OCR recognize_captcha 保留签名适配，其余通过类接口提供。

| 旧定义 | 旧行号 | V2 位置 / 处理 |
|---|---:|---|
| `JRQueryResult` | 207 | `telegram_bot/models/query.py` |
| `JRCachedResult` | 219 | `telegram_bot/models/query.py` |
| `checkin_bot_key` | 228 | `telegram_bot/target_bots.py` |
| `migrate_last_sign_time_schema` | 233 | `telegram_bot/storage/migrations.py` |
| `init_db` | 295 | `telegram_bot/storage/database.py` |
| `close_db` | 354 | `telegram_bot/storage/database.py` |
| `get_last_sign_time` | 360 | `telegram_bot/storage/repositories/checkin_repository.py` |
| `set_last_sign_time` | 370 | `telegram_bot/storage/repositories/checkin_repository.py` |
| `normalize_jr_query` | 379 | `telegram_bot/services/jr/cache_service.py` |
| `jr_cache_key` | 384 | `telegram_bot/services/jr/cache_service.py` |
| `get_jr_query_lock` | 389 | `telegram_bot/utils/keyed_locks.py` |
| `hold_jr_query_lock` | 400 | `telegram_bot/utils/keyed_locks.py` |
| `_is_path_in_jr_cache` | 422 | `telegram_bot/utils/paths.py` |
| `remove_jr_cached_file` | 430 | `telegram_bot/services/jr/cache_service.py` |
| `cleanup_orphan_jr_cache_files` | 448 | `telegram_bot/services/jr/cache_service.py` |
| `get_jr_cached_result` | 472 | `telegram_bot/services/jr/cache_service.py` |
| `set_jr_cached_result` | 499 | `telegram_bot/services/jr/cache_service.py` |
| `purge_expired_jr_cache` | 538 | `telegram_bot/services/jr/cache_service.py` |
| `get_jr_membership` | 572 | `telegram_bot/storage/repositories/membership_repository.py` |
| `set_jr_membership` | 584 | `telegram_bot/storage/repositories/membership_repository.py` |
| `jr_usage_date` | 596 | `telegram_bot/storage/repositories/membership_repository.py` |
| `get_jr_daily_usage` | 601 | `telegram_bot/storage/repositories/membership_repository.py` |
| `increment_jr_daily_usage` | 612 | `telegram_bot/storage/repositories/membership_repository.py` |
| `get_jr_account_state_snapshot` | 624 | `telegram_bot/storage/repositories/membership_repository.py` |
| `is_user_banned` | 657 | `telegram_bot/storage/repositories/user_repository.py` |
| `unban_user` | 667 | `removed: command retired` |
| `get_phone` | 677 | `telegram_bot/clients/userbot_manager.py` |
| `get_client_lock` | 681 | `telegram_bot/clients/userbot_manager.py` |
| `reply_long` | 687 | `telegram_bot/utils/message_format.py` |
| `reply_text_retry` | 708 | `telegram_bot/utils/retry.py` |
| `require_admin` | 719 | `telegram_bot/handlers/guards.py` |
| `banned_command_guard` | 729 | `telegram_bot/handlers/guards.py` |
| `load_clients` | 744 | `telegram_bot/clients/userbot_manager.py` |
| `start_all_clients` | 766 | `telegram_bot/clients/userbot_manager.py` |
| `build_ad_available_clients` | 799 | `removed: obsolete broadcast system` |
| `do_checkin` | 809 | `telegram_bot/services/checkin_service.py` |
| `query_balance` | 890 | `telegram_bot/services/balance_service.py` |
| `_wait_for_bot_reply` | 971 | `telegram_bot/clients/dialogue.py` |
| `auto_checkin_scheduler` | 987 | `telegram_bot/services/checkin_service.py` |
| `get_next_ad_client` | 1046 | `removed: obsolete broadcast system` |
| `wait_for_next_ad_client` | 1061 | `removed: obsolete broadcast system` |
| `parse_tg_link` | 1087 | `removed: obsolete broadcast system` |
| `ad_loop` | 1093 | `removed: obsolete broadcast system` |
| `target_message_signature` | 1197 | `telegram_bot/clients/dialogue.py` |
| `is_verify_success` | 1209 | `telegram_bot/services/jr/result_parser.py` |
| `is_jr_pending_message` | 1214 | `telegram_bot/services/jr/result_parser.py` |
| `wait_jr_ready_reply` | 1220 | `telegram_bot/services/jr/target_client.py` |
| `wait_target_after_click` | 1278 | `telegram_bot/services/jr/target_client.py` |
| `normalize_ocr_text` | 1303 | `telegram_bot/services/jr/ocr_service.py` |
| `extract_ocr_candidates` | 1312 | `telegram_bot/services/jr/ocr_service.py` |
| `_pil_to_png_bytes` | 1327 | `telegram_bot/services/jr/ocr_service.py` |
| `_classification_with_confidence` | 1333 | `telegram_bot/services/jr/ocr_service.py` |
| `_recognize_captcha_sync` | 1372 | `telegram_bot/services/jr/ocr_service.py` |
| `recognize_captcha` | 1458 | `telegram_bot/services/jr/ocr_service.py` |
| `choose_ocr_button` | 1479 | `telegram_bot/services/jr/orchestrator.py：由 MANUAL 挂起替代自动点击；离线识别保留于 ocr_service` |
| `solve_verification_with_ocr` | 1533 | `telegram_bot/services/jr/orchestrator.py：由 MANUAL 挂起替代自动点击；离线识别保留于 ocr_service` |
| `jr_cache_cleanup_loop` | 1601 | `telegram_bot/services/jr/cache_service.py` |
| `_button_text` | 1619 | `telegram_bot/services/jr/result_parser.py` |
| `is_verification_message` | 1622 | `telegram_bot/services/jr/result_parser.py` |
| `find_button` | 1655 | `telegram_bot/clients/dialogue.py` |
| `find_export_button` | 1666 | `telegram_bot/services/jr/export_service.py` |
| `get_callback_answer_text` | 1671 | `telegram_bot/services/jr/export_service.py` |
| `is_free_export_feedback` | 1675 | `telegram_bot/services/jr/export_service.py` |
| `is_jr_export_file_message` | 1679 | `telegram_bot/services/jr/export_service.py` |
| `jr_message_reply_to_id` | 1690 | `telegram_bot/services/jr/export_service.py` |
| `jr_message_matches_export_request` | 1699 | `telegram_bot/services/jr/export_service.py` |
| `jr_original_file_name` | 1722 | `telegram_bot/services/jr/export_service.py` |
| `download_jr_export_file` | 1730 | `telegram_bot/services/jr/export_service.py` |
| `detect_jr_membership` | 1753 | `telegram_bot/services/jr/membership_service.py` |
| `try_jr_export_file` | 1776 | `telegram_bot/services/jr/export_service.py` |
| `find_buy_button` | 1893 | `telegram_bot/services/jr/result_parser.py` |
| `find_next_page_button` | 1919 | `telegram_bot/services/jr/result_parser.py` |
| `parse_page_info` | 1935 | `telegram_bot/services/jr/result_parser.py` |
| `clean_page_text` | 1947 | `telegram_bot/services/jr/result_parser.py` |
| `page_content_signature` | 1952 | `telegram_bot/services/jr/result_parser.py` |
| `wait_message_change` | 1966 | `telegram_bot/services/jr/target_client.py` |
| `fetch_all_paged_data` | 2022 | `telegram_bot/services/jr/pagination.py` |
| `click_buy_and_fetch_all` | 2145 | `telegram_bot/services/jr/orchestrator.py` |
| `strip_backticks` | 2211 | `telegram_bot/services/jr/result_parser.py` |
| `normalize_jr_full_result` | 2216 | `telegram_bot/services/jr/result_parser.py` |
| `split_text_by_lines` | 2258 | `telegram_bot/utils/message_format.py` |
| `send_long_text_to_admin` | 2299 | `telegram_bot/services/jr/export_service.py` |
| `send_jr_file_to_admin` | 2340 | `telegram_bot/services/jr/export_service.py` |
| `parse_balance_number` | 2354 | `telegram_bot/services/balance_service.py` |
| `rank_jr_clients_by_balance` | 2367 | `telegram_bot/services/jr/account_selector.py` |
| `record_jr_query_success` | 2415 | `telegram_bot/storage/repositories/query_repository.py` |
| `jr_try_client` | 2448 | `telegram_bot/services/jr/orchestrator.py` |
| `run_jr_query` | 2512 | `telegram_bot/services/jr/orchestrator.py` |
| `run_jr_query_locked` | 2534 | `telegram_bot/services/jr/orchestrator.py` |
| `jr_command` | 2643 | `telegram_bot/handlers/jr_query.py` |
| `jr_plus_handler` | 2651 | `telegram_bot/handlers/jr_query.py` |
| `start_command` | 2671 | `telegram_bot/handlers/start_help.py` |
| `help_command` | 2700 | `telegram_bot/handlers/start_help.py` |
| `unban_command` | 2705 | `removed: command retired` |
| `qd_command` | 2719 | `telegram_bot/handlers/checkin.py` |
| `jf_command` | 2759 | `telegram_bot/handlers/balance.py` |
| `status_command` | 2797 | `telegram_bot/handlers/status.py` |
| `dg_command` | 2828 | `removed: obsolete broadcast system` |
| `tz_command` | 2849 | `removed: obsolete broadcast system` |
| `ad_content_handler` | 2876 | `removed: obsolete broadcast system` |
| `error_handler` | 2892 | `telegram_bot/handlers/registration.py` |
| `configure_bot_command_menus` | 2896 | `telegram_bot/handlers/registration.py` |
| `cancel_background_tasks` | 2955 | `telegram_bot/lifecycle.py` |
| `cleanup_bot_application` | 2971 | `telegram_bot/clients/bot_application.py` |
| `main` | 3014 | `telegram_bot/bootstrap.py` |

## 全局状态迁移

客户端列表/连接状态/锁/并发 → UserbotManager；数据库连接/事务锁 → Database；签到任务 → CheckinService + TaskRegistry；OCR 引擎/线程池 → OCRService；查询锁/引用数 → KeyedLocks；查询意图/用量预留/消息 ID → QueryRepository；Bot Application → BotApplication，由 Lifecycle 关闭。旧广告配置、群组、内容、索引、任务、冷却和停止事件全部删除。配置 → Config / target_bots；不复制凭据。
