# R5 组队系统 — 前端 API 文档

> **Base URL:** `http://{HOST}:{PORT}/v1/r5`
>
> **统一响应格式:**
>
> ```json
> { "code": "0000", "data": <any>, "msg": "string" }
> ```
>
> 分页接口额外返回 `"total": number`

---

## 一、认证方式

### 前端接口 — AppKey 认证

在 HTTP Header 中传递：

```
X-App-Key: <用户的AppKey>
```

AppKey 由用户通过 QQ 机器人私信 `绑定 <游戏昵称或ID>` 获得。

### 后台/Bot 接口 — Bearer Token 认证

```
Authorization: Bearer <service_token>
```

> 前端只需关注 **AppKey 认证**的接口（下文标注为 `AppKey`），Bearer Token 接口由 Bot 内部调用。

---

## 二、用户接口

### 2.1 获取个人信息

```
GET /user/me
```

**认证:** AppKey

**Response:**

```json
{
  "code": "0000",
  "msg": "OK",
  "data": {
    "id": 1,
    "platform": "qq",
    "platform_uid": "123456789",
    "player_id": 42,
    "player_name": "SomePlayer",
    "nucleus_id": 1000000001
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 绑定记录 ID |
| `platform` | string | 平台标识 (`"qq"` / `"kaiheila"`) |
| `platform_uid` | string | 平台用户 ID (QQ号) |
| `player_id` | int | 数据库玩家 ID |
| `player_name` | string | 游戏内昵称 |
| `nucleus_id` | int \| null | 游戏 Nucleus ID |

**错误:**

| HTTP | code | 说明 |
|------|------|------|
| 401 | — | AppKey 无效 (HTTP 层直接返回 401) |
| 200 | `6003` | 绑定记录异常 |

---

## 三、组队接口

### 通用数据结构

#### Team 对象

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 队伍 ID |
| `creator` | MemberInfo | 队长信息 |
| `slots_needed` | int | 创建时设定的缺人数 (1 或 2) |
| `slots_remaining` | int | 当前还缺几人 |
| `status` | string | `"open"` / `"full"` / `"cancelled"` / `"expired"` |
| `members` | MemberInfo[] | 所有成员列表 |
| `created_at` | string | ISO 8601 创建时间 |

#### MemberInfo 对象

| 字段 | 类型 | 说明 |
|------|------|------|
| `binding_id` | int | 绑定记录 ID |
| `platform` | string | `"qq"` / `"kaiheila"` (仅机器人/内部带 Token 访问时返回) |
| `platform_uid` | string | QQ号 (仅机器人/内部带 Token 访问时返回) |
| `player_id` | int | 数据库玩家 ID |
| `player_name` | string | 游戏昵称 |
| `kd` | float | 玩家总 KD |
| `role` | string | `"creator"` / `"member"` (仅 members 数组中) |
| `joined_at` | string \| null | ISO 8601 加入时间 (仅 members 数组中) |

---

### 3.1 获取组队列表 (公开)

```
GET /teams?page_no=1&page_size=20
```

**认证:** 无需

**Query 参数:**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `page_no` | int | 1 | 页码, >=1 |
| `page_size` | int | 20 | 每页条数, 1-100 |

**Response:**

```json
{
  "code": "0000",
  "msg": "Teams retrieved",
  "total": 5,
  "data": [
    {
      "id": 1,
      "creator": {
        "binding_id": 1,
        "player_id": 42,
        "player_name": "PlayerA",
        "kd": 2.15
      },
      "slots_needed": 2,
      "slots_remaining": 1,
      "status": "open",
      "members": [
        {
          "binding_id": 1,
          "player_id": 42,
          "player_name": "PlayerA",
          "kd": 2.15,
          "role": "creator",
          "joined_at": "2026-04-10T12:00:00"
        },
        {
          "binding_id": 3,
          "player_id": 88,
          "player_name": "PlayerB",
          "kd": 1.80,
          "role": "member",
          "joined_at": "2026-04-10T12:05:00"
        }
      ],
      "created_at": "2026-04-10T12:00:00"
    }
  ]
}
```

> 列表按创建者 KD 降序排列。
> 公开访问不会返回 `platform` 和 `platform_uid`；机器人/内部带 Bearer Token 访问时会返回完整字段。

---

### 3.2 获取队伍详情 (公开)

```
GET /teams/{team_id}
```

**认证:** 无需

**Path 参数:**

| 参数 | 类型 | 说明 |
|------|------|------|
| `team_id` | int | 队伍 ID |

**Response:** 单个 Team 对象, 结构同 3.1。

```json
{
  "code": "0000",
  "msg": "OK",
  "data": { /* Team 对象 */ }
}
```

> 公开访问不会返回 `platform` 和 `platform_uid`；机器人/内部带 Bearer Token 访问时会返回完整字段。

**错误:**

| code | 说明 |
|------|------|
| `7001` | 队伍不存在 |

---

### 3.3 创建组队

```
POST /teams/app/create?slots_needed={1|2}
```

**认证:** AppKey

**Query 参数:**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `slots_needed` | int | 是 | 缺人数: `1` 或 `2` (队伍总人数 = slots_needed + 1) |

**Response:**

```json
{
  "code": "0000",
  "msg": "组队创建成功",
  "data": { /* Team 对象 */ }
}
```

**错误:**

| code | 说明 |
|------|------|
| `7003` | 你已有进行中的队伍, 请先取消或退出 |
| `7005` | slots_needed 不合法 (只能是 1 或 2) |

---

### 3.4 加入队伍

```
POST /teams/app/{team_id}/join
```

**认证:** AppKey

**Path 参数:**

| 参数 | 类型 | 说明 |
|------|------|------|
| `team_id` | int | 要加入的队伍 ID |

**Response (未满员):**

```json
{
  "code": "0000",
  "msg": "加入成功",
  "data": {
    "team": { /* Team 对象 */ },
    "notify_members": null
  }
}
```

**Response (加入后满员):**

```json
{
  "code": "0000",
  "msg": "加入成功",
  "data": {
    "team": { "status": "full", "..." : "..." },
    "notify_members": [
      {
        "platform": "qq",
        "platform_uid": "123456789",
        "player_name": "PlayerA",
        "kd": 2.15,
        "role": "creator"
      },
      {
        "platform": "qq",
        "platform_uid": "987654321",
        "player_name": "PlayerB",
        "kd": 1.80,
        "role": "member"
      },
      {
        "platform": "qq",
        "platform_uid": "111222333",
        "player_name": "PlayerC",
        "kd": 1.50,
        "role": "member"
      }
    ]
  }
}
```

**notify_members 数组项:**

| 字段 | 类型 | 说明 |
|------|------|------|
| `platform` | string | 平台标识 |
| `platform_uid` | string | QQ号 |
| `player_name` | string | 游戏昵称 |
| `kd` | float | 玩家总 KD |
| `role` | string | `"creator"` / `"member"` |

> 前端可根据 `notify_members !== null` 判断队伍是否满员, 展示匹配成功页面。

**错误:**

| code | 说明 |
|------|------|
| `7001` | 队伍不存在或已关闭 |
| `7002` | 队伍已满 |
| `7003` | 你已有进行中的队伍 |
| `7006` | 不能加入自己创建的队伍 |

---

### 3.5 取消组队 (仅队长)

```
POST /teams/app/{team_id}/cancel
```

**认证:** AppKey

**Path 参数:**

| 参数 | 类型 | 说明 |
|------|------|------|
| `team_id` | int | 要取消的队伍 ID |

**Response:**

```json
{ "code": "0000", "data": null, "msg": "组队已取消" }
```

**错误:**

| code | 说明 |
|------|------|
| `7001` | 队伍不存在或已关闭 |
| `7004` | 只有队长可以取消组队 |

---

### 3.6 退出队伍 (非队长成员)

```
POST /teams/app/{team_id}/leave
```

**认证:** AppKey

**Path 参数:**

| 参数 | 类型 | 说明 |
|------|------|------|
| `team_id` | int | 要退出的队伍 ID |

**Response:**

```json
{ "code": "0000", "data": null, "msg": "已退出队伍" }
```

**错误:**

| code | 说明 |
|------|------|
| `7001` | 队伍不存在或已关闭 |
| `7004` | 队长不能退出队伍, 请使用取消组队 |
| `7007` | 你不在该队伍中 |

---

## 四、错误码速查表

| code | 常量名 | 含义 |
|------|--------|------|
| `0000` | SUCCESS | 成功 |
| `6001` | BINDING_PLAYER_NOT_FOUND | 未找到匹配的游戏玩家 |
| `6002` | BINDING_ALREADY_EXISTS | 该平台账号已绑定其他玩家 |
| `6003` | BINDING_NOT_FOUND | 未绑定 / AppKey 无效 |
| `6004` | BINDING_PLAYER_AMBIGUOUS | 匹配到多个玩家, 需更精确的昵称或ID |
| `7001` | TEAM_NOT_FOUND | 队伍不存在或已关闭 |
| `7002` | TEAM_ALREADY_FULL | 队伍已满 |
| `7003` | TEAM_ALREADY_IN_TEAM | 用户已在其他队伍中 |
| `7004` | TEAM_NOT_CREATOR | 非队长无权操作 |
| `7005` | TEAM_INVALID_SLOTS | slots_needed 参数不合法 |
| `7006` | TEAM_CANNOT_JOIN_OWN | 不能加入自己创建的队伍 |
| `7007` | TEAM_NOT_MEMBER | 不在该队伍中 |

---

## 五、前端接口汇总

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| `GET` | `/user/me` | AppKey | 获取当前用户信息 |
| `GET` | `/teams` | 无 | 组队列表 (分页, 按KD排序) |
| `GET` | `/teams/{team_id}` | 无 | 队伍详情 |
| `POST` | `/teams/app/create?slots_needed=N` | AppKey | 创建组队 |
| `POST` | `/teams/app/{team_id}/join` | AppKey | 加入队伍 |
| `POST` | `/teams/app/{team_id}/cancel` | AppKey | 取消组队 (队长) |
| `POST` | `/teams/app/{team_id}/leave` | AppKey | 退出队伍 (成员) |

---

## 六、典型前端流程

### 登录流程

```
用户输入 AppKey → GET /user/me → 成功则进入主页, 失败提示重新绑定
```

### 创建组队流程

```
用户选择缺人数 → POST /teams/app/create?slots_needed=1
→ 成功后跳转到队伍详情页, 轮询等待队友加入
```

### 加入队伍流程

```
GET /teams → 展示列表 → 用户点击加入
→ POST /teams/app/{team_id}/join
→ 检查 notify_members:
   - null: 显示等待中
   - 非null: 显示匹配成功, 展示队友信息
```

### 轮询建议

队伍详情页建议每 **5 秒** 轮询 `GET /teams/{team_id}` 检查状态变化:
- `status` 变为 `"full"` → 匹配成功
- `status` 变为 `"cancelled"` → 队伍已取消
- `slots_remaining` 变化 → 有新队友加入
