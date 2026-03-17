# 登录脚本说明（固定 URL + 固定滑块 XPath）

脚本文件：`scripts_sim_login.py`

## 入参

仅需传：
- `--username`
- `--password`
- `--headless / --no-headless`

（URL 和滑块 XPath 已写死在代码中）

## 运行示例

```bash
python scripts_sim_login.py \
  --username "czzjspt_admin" \
  --password "czzjspt_admin@123B.." \
  --no-headless
```

## 输出

脚本只输出最终结果：

```text
access_token=xxxxx
```

提取失败时：

```text
access_token=
```

## token 提取策略

1. 点击登录后，优先等待并解析固定登录接口：`POST http://cloud.lihero.com:3700/java2/auth/login`。
2. 从接口返回体中提取 `access_token / accessToken / token`。
3. 若接口中未取到，再从 localStorage/sessionStorage/cookie 回退提取。
