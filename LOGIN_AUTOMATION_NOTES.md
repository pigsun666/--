# 统计采集脚本说明（固定登录 + 固定 4 个统计接口）

脚本文件：`scripts_sim_login.py`

## 入参

必传：
- `--username`
- `--password`

可选：
- `--headless / --no-headless`
- `--start-date YYYY-MM-DD`
- `--end-date YYYY-MM-DD`

## 时间范围

- 如果 `--start-date` 和 `--end-date` 都不传，默认查询**近一周**。
- 如果只传 `--start-date`，结束日期默认取今天。
- 如果只传 `--end-date`，开始日期默认取该日期往前 7 天。

## 运行示例

默认近一周：

```bash
python scripts_sim_login.py \
  --username "czzjspt_admin" \
  --password "czzjspt_admin@123B.." \
  --no-headless
```

指定时间范围：

```bash
python scripts_sim_login.py \
  --username "czzjspt_admin" \
  --password "czzjspt_admin@123B.." \
  --start-date 2026-03-01 \
  --end-date 2026-03-21 \
  --no-headless
```

## 输出

脚本最终输出一份 JSON，包含：

- `overview`：总体统计均值
- `sites`：按站点聚合后的完整字段
- `raw_files`：4 个原始接口返回值落盘路径
- `requests`：4 个接口的请求状态
- `date_range`：本次实际查询时间范围

## 认证与请求

- 登录接口：`POST http://cloud.lihero.com:3700/java2/auth/login`
- 业务接口请求头：`Authorization: Bearer <token>`
- 固定抓取 4 个统计接口：
  1. 数据捕获情况
  2. 数据审核率统计
  3. 质控完成率
  4. 运维完成率

## 原始数据

原始接口返回值不会直接塞进主输出里，而是落盘到 `artifacts/` 目录，并通过 `raw_files` 字段返回路径。
