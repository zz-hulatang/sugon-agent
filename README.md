# OA 智能助手

Streamlit + LangChain + Playwright + MySQL。目前支持自动登录、MySQL 认证会话保存、历史聊天记录与续聊、当前用户信息查询、异常考勤查询与提交，以及只读请求认证失效后自动登录并继续原任务。异常考勤真实查询和提交前必填校验已验证，最终提交通过模拟验证，尚未执行真实审批写入。

## 本机运行

使用 Python 3.11+，需要能够访问 OA 的网络环境及桌面显示环境。已有系统 Chrome 时可跳过 Chromium 下载；默认优先使用 Playwright Chromium，否则使用系统 Chrome。均使用独立临时上下文，不复用系统浏览器已有登录：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
PLAYWRIGHT_BROWSERS_PATH=0 python -m playwright install chromium --no-shell
cp -n .env.example .env
python scripts/configure_mysql.py
streamlit run apps/streamlit_app.py --server.address 127.0.0.1
```

在 `.env` 设置 `OPENAI_API_KEY`、`OPENAI_MODEL`（支持工具调用的模型），可选设置 `OPENAI_BASE_URL`。不配置模型也能使用侧栏登录功能。

首次运行还需按下一节配置 MySQL，并创建 `oa_agent` 数据库。已有 `.env` 不要覆盖，配置脚本仅追加缺失配置并生成本机加密密钥。

先在项目根目录执行上述安装命令，将 `src/oa_agent` 以可编辑包安装到虚拟环境。依赖统一维护在 `pyproject.toml`；`pip install -r requirements.txt` 也会安装同一个项目。原命令 `streamlit run main.py` 继续可用。

### 使用 PyCharm 已选择的 Anaconda 解释器

如果项目解释器是 `/opt/anaconda3/bin/python`，可以继续使用它，在项目根目录执行：

```bash
/opt/anaconda3/bin/python -m pip install -e .
/opt/anaconda3/bin/python scripts/configure_mysql.py
/opt/anaconda3/bin/python -m streamlit run apps/streamlit_app.py --server.address 127.0.0.1
```

PyCharm 的 Python 运行配置使用模块名 `streamlit`，参数为 `run apps/streamlit_app.py --server.address 127.0.0.1`，工作目录为项目根目录，解释器使用项目的 Anaconda。`src` 布局需要将项目安装到运行它的解释器中；直接运行 `main.py` 不会启动 Streamlit 服务。

1. 点击“打开 OA 登录”，或者在对话中说“打开 OA，让我登录”。
2. 若已配置账号密码，后端自动填写并提交；否则在弹出的独立浏览器中手动登录。短信验证码、扫码或 MFA 由用户在浏览器中完成。
3. 登录成功后自动采集并保存最新 Cookie / Token 到 MySQL，无需点击确认或刷新。
4. 业务请求从 MySQL 读取认证。401 后自动重新登录、更新数据库并继续原查询，无需再次输入任务。
5. “关闭会话并清除凭据”删除该用户的数据库会话并关闭本地浏览器，不等同于 OA 服务端注销。正常退出应用或浏览器空闲清理会保留数据库会话，供下次请求复用。

浏览器在 **Streamlit 所在电脑** 上打开。本实现面向本机使用，不适合直接部署为远程多用户服务；普通网页链接无法读取另一个浏览器的 HttpOnly Cookie。

## Docker 构建与运行

需要 Docker Engine / Docker Desktop 和 Docker Compose v2。镜像包含 Python、项目依赖、Playwright Chromium 及虚拟桌面，支持在容器中完成原有的可见浏览器登录。浏览器与系统依赖在构建时安装，方式参见 [Playwright 官方文档](https://playwright.dev/python/docs/docker)。构建需要访问 Python、Debian 和 Playwright 下载源；运行时需要能够从容器访问 OA、模型接口和 MySQL。

先按下节创建数据库，并准备项目根目录的 `.env`；已有配置可以复用：

```bash
cp -n .env.example .env
# 已安装本机依赖时，可追加默认数据库配置并生成加密密钥：
python scripts/configure_mysql.py
docker compose up -d --build
docker compose ps
```

没有本机 Python 环境时，也可以先执行 `docker compose build`，再用镜像生成密钥，将输出填入 `.env` 的 `SESSION_ENCRYPTION_KEY`（只生成一次并妥善保存）：

```bash
docker run --rm --entrypoint python sugon-oa-agent:local -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

- 应用：[http://localhost:8501](http://localhost:8501)。
- 登录桌面：[http://localhost:6080/vnc.html?autoconnect=true&resize=scale](http://localhost:6080/vnc.html?autoconnect=true&resize=scale)。先在应用中点击“打开 OA 登录”，再到桌面中填写账号、扫码或输入验证码；已配置账号密码时仍可在此完成二次验证。
- 查看日志：`docker compose logs -f app`；停止并移除容器：`docker compose down`。数据库中的会话仍保留，重新启动时继续使用原来的加密密钥。

Compose 默认连接宿主机 MySQL（`host.docker.internal`），会覆盖 `.env` 中的 `MYSQL_HOST=localhost`。数据库需监听容器可访问的地址，并允许应用账号从容器网络连接。连接其他服务器或同网络数据库服务时，在 `.env` 增加 `DOCKER_MYSQL_HOST=实际主机名或IP`；数据库端口仍由 `MYSQL_PORT` 控制。MySQL 由现有服务提供，Compose 不自动创建数据库。使用 `MYSQL_SSL_CA` 时，需通过 Compose 的只读 volume 挂载 CA 文件，并将该变量设置为容器内路径。

`.env` 仅在启动时注入环境变量，不复制进镜像；镜像以普通用户运行。Compose 固定使用镜像自带的 Chromium，不使用宿主机的 Chrome 或 `.env` 中的浏览器安装路径。修改 `.env` 后执行 `docker compose up -d --force-recreate`。

应用和桌面端口默认只绑定宿主机 `127.0.0.1`。桌面没有单独的登录密码，会显示该容器内的全部浏览器窗口；此配置供单用户本机使用，请勿直接向公网开放或共用给多个用户。容器浏览器不会在宿主机弹窗，应通过上述桌面入口操作。

如需只构建镜像：

```bash
docker build -t sugon-oa-agent:local .
```

基础镜像默认使用 [Docker 官方在 Amazon ECR 发布的 Python 镜像](https://gallery.ecr.aws/docker/library/python)，以避开 Docker Hub 连接超时。若构建在 `load metadata` / `failed to authorize` 阶段超时，可先运行 `docker pull public.ecr.aws/docker/library/python:3.12-slim-bookworm` 检查镜像仓库连通性。需要改回 Docker Hub 或使用内部仓库时，在 `.env` 设置 `DOCKER_PYTHON_IMAGE=python:3.12-slim-bookworm`（或内部镜像地址）；直接构建则使用 `docker build --build-arg PYTHON_IMAGE=python:3.12-slim-bookworm -t sugon-oa-agent:local .`。

直接使用 `docker run` 时需传入 `.env`、设置 `--shm-size=1g`、发布两个本机端口，并覆盖 `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright`、`OA_BROWSER_CHANNEL=` 和 `MYSQL_HOST`；推荐使用已包含这些设置的 Compose 配置。

## MySQL 数据源和任务自动恢复

默认主机为 `localhost:3306`，数据库名为 `oa_agent`。先使用现有 MySQL 管理账号创建数据库：

```sql
CREATE DATABASE IF NOT EXISTS oa_agent CHARACTER SET utf8mb4;
```

在本机 `.env` 填写数据库连接账号和密码（无需发给模型）：

```dotenv
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_DATABASE=oa_agent
MYSQL_USER=你的数据库账号
MYSQL_PASSWORD='你的数据库密码'
MYSQL_SSL_CA=
SESSION_ENCRYPTION_KEY=由configure_mysql.py生成的固定密钥
OA_RELOGIN_TIMEOUT=180
```

`python scripts/configure_mysql.py` 会追加默认配置并生成缺失密钥，保留已有密码和非空密钥；不会创建数据库账号。应用首次连接时自动创建 `oa_auth_sessions`，SQL 另见 `sql/001_auth_sessions.sql`。数据库账号需有库内 `CREATE / SELECT / INSERT / UPDATE / DELETE` 权限。`MYSQL_SSL_CA` 可指定数据库 TLS CA 文件，启用证书和主机名校验。

每个用户名只保存一份最新认证记录，Cookie 属性、Token 来源、业务 Token 和采集时间以 Fernet 密文存储，数据库不保存 OA 密码。加密密钥保存在本机配置中；更换密钥后，旧记录需清理并重新登录。

业务链路为：**读数据库 → 使用数据库 Cookie/Token 发起请求 → 401 → 按版本使旧记录失效 → 自动登录并入库 → 再读数据库 → 继续原请求**。请求使用独立的 Playwright API 上下文，不混入浏览器中可能过期的 Cookie。数据库不可用时返回错误，不会回退到旧内存凭据。

没有数据库会话时也会自动打开浏览器登录。短信验证码和 MFA 由用户在浏览器完成，后端保留原任务等待验证；默认等待 180 秒，可通过 `OA_RELOGIN_TIMEOUT` 调整。登录成功后直接返回原任务结果，不要求用户再次输入查询。对话在后台运行，Streamlit 重跑仅刷新进度，不重复调用模型；完整关闭页面/WebSocket 或重启进程不提供任务恢复保证。

一次查询最多自动恢复登录一次；重新登录后仍返回 401、登录失败、用户关闭窗口/取消或超过等待上限时返回失败结果，避免无限重试。并发请求使用记录版本号删除旧凭据，不删除其他会话刚更新的新版本。当前自动重放用于员工详情及异常考勤的只读查询，写入和提交不自动重放。

## 历史会话记录

侧栏“历史会话”按最近更新时间展示记录，每次加载 30 条，可继续加载更多。点击“新建会话”打开空白聊天，首次发送时自动保存，以第一条问题的前 30 字作为标题。点击旧会话即可恢复上下文续聊；刷新页面或重启应用后默认打开最近会话。删除需在侧栏确认，执行中的会话暂时不能删除。

记录按本机配置的 `OA_USERNAME` 归档，使用现有 MySQL 连接，首次访问自动创建 `oa_chat_conversations` 和 `oa_chat_messages`，也可手动执行 `sql/002_chat_history.sql`。消息正文使用现有 `SESSION_ENCRYPTION_KEY` 加密，标题和更新时间用于历史列表；更换密钥将无法读取原正文。未配置 `OA_USERNAME` 时只保留当前页面的临时聊天。当前应用仍面向单用户本机使用，配置账号用于记录归属，不新增网页登录认证。

同一页面同时执行一个任务，期间可切换查看或新建会话；后台结果始终保存到原会话。数据库暂时不可用时，聊天继续，界面明确显示未保存提示，并每 10 秒重试保存，也可点击“刷新并重试保存”。重试使用固定消息 ID，不重复调用模型或 OA 工具。未保存内容仍在进程内，关闭页面或重启进程可能丢失；进程中仍存活的后台任务会继续尝试落库。

页面重跑不重复执行任务，进程重启也不会自动重放历史任务。只有问题而没有回复的会话会显示未完成提示；涉及提交时应先在 OA 核实结果。恢复历史不恢复浏览器任务或考勤快照，考勤快照仍按原有有效期和提交规则处理。记录保留至用户删除，显示时间为上海时区；删除清除消息正文并保留删除标记，防止旧补存请求重新创建会话。

测试命令：

```bash
.venv/bin/python -m unittest discover -s tests
# 使用独立测试 MySQL，库名/测试账号见脚本说明，不读取项目 .env：
MYSQL_SMOKE_PORT=你的测试端口 .venv/bin/python tests/integration/mysql_history_smoke.py
```

## 账号密码自动登录

在项目根目录的 `.env` 中增加以下两项，然后**重启 Streamlit**（已有会话保留创建时的配置）：

```dotenv
OA_USERNAME='你的账号'
OA_PASSWORD='你的密码'
```

同时配置后，点击“打开 OA 登录”或让 Agent 调用 `oa_login`，即可自动打开登录页、填写账号密码和提交。两项都为空时保留手动登录；只配置一项会报配置错误。密码包含 `#`、空格等字符时应使用引号；包含引号、变量展开语法等复杂内容时也可直接通过进程环境变量传入。

已读取[公开登录页](https://soa.com.cn/sioLogin/)及其[登录交互脚本](https://soa.com.cn/sioLogin/login.js?v=sms-submit-20261025)：默认填写 `#username` / `#password`，等待 `#login-slider` 启用，再使用键盘 End 将普通 range 登录控件移到末端，触发页面自身的登录处理。页面的加密、认证及短信流程仍由 OA 原有脚本完成。真实账号登录尚未联调。

- 仅在配置登录地址的同源、同路径页面或 frame 中填写凭据，不向其他 OA 页面或第三方 frame 填写密码。
- 同一浏览器会话最多自动提交一次；失败后停止自动重试。可手动处理，或关闭会话后重新打开；修改账号密码后需重启应用。
- 出现短信验证码或 MFA 时侧栏显示“需要二次验证”，用户在浏览器完成后继续自动采集，不需要采集确认按钮。
- 账号密码不作为工具参数，不进入模型提示词、工具结果或聊天历史，也不输出原始浏览器异常。

默认无需修改表单规则；页面改版时可在 `.env` 调整：

```dotenv
OA_LOGIN_URL=https://soa.com.cn/sioLogin/
OA_USERNAME_SELECTOR='#username'
OA_PASSWORD_SELECTOR='#password'
OA_SUBMIT_SELECTOR='#login-slider'
OA_VERIFICATION_SELECTOR='#sms-panel:visible, input[autocomplete="one-time-code"]:visible'
OA_LOGIN_ERROR_SELECTOR='#login-error:visible'
```

`OA_SUBMIT_SELECTOR` 指向 range 时使用键盘提交，指向按钮时执行点击。验证/错误选择器应匹配可见元素。自定义登录主机还需加入 `OA_EXTRA_HOSTS`；自动填表不依赖 `OA_SUCCESS_SELECTOR` / `OA_SUCCESS_URL_REGEX`，登录成功与会话采集仍使用下一节的检测规则。

## 登录检测与 OA 适配

已核实公开入口跳转：`https://soa.com.cn/` → `/portalpage/` → `https://sio.soa.com.cn/`（Sio智能办公）。[公开前端脚本](https://sio.soa.com.cn/assets/index-BSTATs5U.js) 从 `DP_Token` Cookie 读取并 URL 解码 Token，然后在请求中使用 `token` 头（部分请求使用 `Token`）。已添加 `DP_Token` 提取；Cookie 原值保持不变。该信息来自静态脚本，尚未使用真实账号验证登录和实际请求。脚本还引用了 `https://soa.com.cn/sioLogin/` 登录入口。

- `OA_SUCCESS_SELECTOR`：登录后才出现的 CSS 元素，例如实际系统的退出按钮。
- `OA_SUCCESS_URL_REGEX`：登录成功 URL 正则。若两项均配置，需同时通过。
- 两项均为空时，监听 OA 页面自身发出的登录用户接口 `https://msp.soa.com.cn/api/sap/getLoginUser`：HTTP 200、业务状态成功、有效用户标识（`uname` / `USRID`），且请求 Token 与当前页面可用的 `DP_Token` Cookie 一致后，自动采集并进入 `verified` 状态。不把一般 Cookie 或页面跳转当作登录成功证据。接口返回 401/403 会清除已采集凭据，恢复等待登录。
- `OA_USER_INFO_URL`：可配置上述接口完整 HTTPS 地址，主机必须在允许列表中。接口路径、域名、用户字段及 `token` 请求头来自公开前端脚本，尚待真实账号联调。应用只观察页面发出的请求，不额外轮询接口。
- `OA_EXTRA_HOSTS`：经核实的额外 HTTPS SSO/API 主机名，逗号分隔。默认只采集 `soa.com.cn`、`sio.soa.com.cn` 和用户信息 API 所在的 `msp.soa.com.cn`，不自动信任所有子域名。
- `OA_TOKEN_KEYS`：实际 Token 字段名，支持 Cookie、localStorage、sessionStorage 内嵌 JSON、请求/响应头。
- `OA_IDLE_SECONDS`：从最近一次打开或首次检测到登录成功起计算的清理时间，默认 1800 秒。后台采集和侧栏刷新不会持续续期。
- `OA_BROWSER_CHANNEL`：可显式设置 `chrome` / `msedge`，留空按上述顺序选择。

有些 OA 只使用会话 Cookie，不提供独立 Token；界面会如实显示零项。若 Token 仅存在于响应 JSON、IndexedDB 或特殊 JS 对象，需按真实认证协议补充提取逻辑，目前未实现这些来源。

## 获取当前用户信息

新增 LangChain 工具 `get_user_info`，已与 `oa_login` 一起注册到 Agent。沿用本机 `OA_USERNAME` 配置，登录并自动采集会话成功后，可在聊天中输入：

- “获取我的用户信息。”
- “我的部门和岗位是什么？”
- “查询我的入职日期和公司邮箱。”

工具不接收用户名、密码或 Token 参数。可直接输入查询，由后端复用数据库会话或恢复登录；后端调用：

```text
GET https://msp.soa.com.cn/api/sap/getEmployeeDetail/{URL编码后的OA_USERNAME}
token: 当前会话的业务Token
```

请求从 MySQL 读取最新 Cookie 和业务 Token，在独立 `APIRequestContext` 中保持 Cookie 域名/路径约束。登录入库时，业务 Token 优先使用经过登录用户接口确认的值，其次使用采集的 `DP_Token`。禁止跟随重定向，HTTP 超时为 15 秒。请求和响应原文不写入应用日志。

接口成功响应按员工对象解析，工具返回 `{"status": "success", "message": "已获取当前用户信息。", "data": {...}}`；`data` 保留原有字段和类型（包括工号等字符串中的前导零）。当前实现要求返回的 `uname` 与配置账号一致（忽略大小写）。Agent 根据具体问题使用需要的字段。

未配置账号时返回 `config_error`。缺少会话或首次 401 由后端自动恢复，不提前返回让用户重复输入任务。恢复失败时返回 `auth_required`；恢复后仍为 401 时返回 `auth_required` / `status_code: 401` 并清除该失效版本。数据库、其他 HTTP 错误、无效 JSON 或非员工对象返回 `error`。真实 OA 账号接口尚未联调。

可信后端也可通过 `session.call("user_info")` 调用；请求会在浏览器所属工作线程中执行。

## 异常考勤查询与提交

已注册 `get_attendance_exceptions(year, month)` 和 `submit_abnormal_attendance(draft_id, items)`。先查询异常，按返回的记录和类别选择真实抵消方式，再要求提交；例如“查询 2026 年 11 月的异常考勤”。工具只处理本人和明确选定的记录，实际时间及备注需由用户提供。

每轮对话会向模型注入上海时区的当前日期、本月、上月和下月。可以直接说“查询本月异常考勤”“查一下上个月”或“查我的异常考勤”；模型负责结合上下文填写年月，未提时间且没有相关上下文时默认本月，不要求用户重复提供可推断的年月。查询结果会说明实际年月。

查询快照有效十分钟，提交前刷新记录并核对时长。提交执行原 OA 页面校验、明细保存和审批流程，最终流程接口返回 `STATE=ok` 才算提交成功。提交超时或结果不明返回 `unknown`，应先在 OA 核实；同一快照不重复写入。需要附件或人工审批路由的情况转到原表单办理。

运行仍使用项目的 MySQL 会话，不导入系统 Chrome 的登录资料。已通过实际注册工具验证 `LoginSession → MySQL → 真实 OA` 的身份、异常记录和类别字典查询，并确认页面在缺少抵消类别时阻止提交。已修复旧 OA 将 JSON 响应标为 `text/html` 时误判认证失败的问题。详细字段、接口参数、调用顺序、限制与验证情况见 [调研与接入文档](docs/abnormal-attendance.md)。按用户要求停在提交前，未提交真实 OA 申请，完整审批写入仍待联调。

模拟提交链路测试（使用隔离 Chrome，全量拦截网络，不访问真实 OA）：

```bash
python tests/integration/attendance_smoke.py
```

## 项目结构与职责

```text
oa-agent/
├── apps/
│   └── streamlit_app.py       # 页面、聊天和自动刷新登录状态
├── src/oa_agent/
│   ├── config.py              # 登录/模型配置校验及环境变量加载
│   ├── agents/
│   │   ├── oa_agent.py        # 模型创建和工具注册
│   │   ├── prompts.py         # 系统提示词
│   │   └── state.py           # 聊天消息结构
│   ├── tools/
│   │   ├── login.py           # LangChain oa_login 工具适配
│   │   └── employee.py        # LangChain get_user_info 工具适配
│   ├── services/
│   │   ├── auth.py            # 登录判定、身份响应校验和凭据提取
│   │   ├── auto_login.py      # 本机账号密码填表、一次性提交和二次验证状态
│   │   ├── authenticated_request.py # 读库认证、401 登录恢复和原请求续跑
│   │   └── employee.py        # 当前账号查询、认证前置检查和 401 处理
│   ├── clients/
│   │   ├── browser.py         # Playwright 浏览器创建、事件处理及关闭
│   │   └── oa_client.py       # 员工详情 HTTP 请求及响应解析
│   ├── sessions/
│   │   ├── session.py         # 每个用户独立的工作线程、自动检测和生命周期
│   │   ├── store.py           # 本地展示状态和认证快照
│   │   ├── repository.py      # 仓库协议、版本记录及隔离测试替身
│   │   ├── mysql_repository.py # 加密 MySQL 会话仓库
│   │   └── tasks.py           # 对话后台任务，页面重跑后继续交付结果
│   └── schemas/
│       ├── auth.py            # 凭据、会话状态和状态结果结构
│       └── employee.py        # 员工工具的成功/失败返回结构
├── tests/
│   ├── unit/                 # 认证、配置、模型编排测试
│   ├── integration/          # Streamlit、工具、真实浏览器测试
│   └── fixtures/             # 浏览器测试模拟登录页
├── main.py                   # 兼容启动入口，转到 apps/streamlit_app.py
├── scripts/configure_mysql.py # 追加本机默认配置并生成加密密钥
├── sql/001_auth_sessions.sql  # 会话表结构
├── pyproject.toml            # 包信息、依赖和 src 布局配置
├── requirements.txt          # 转到 pyproject.toml 的兼容安装入口
├── .env.example
└── README.md
```

登录调用链：`Streamlit → oa_login → LoginSession → AuthService / AutoLoginService → MySQL`。用户信息调用链：`Streamlit → get_user_info → LoginSession → EmployeeService → AuthenticatedRequestService → MySQL → OaClient`。`CredentialStore` 仅管理本地展示快照，业务请求以数据库记录为准；模型不接收原始认证凭据。

异常考勤在 `tools/attendance.py`、`services/attendance.py`、`clients/attendance_client.py`、`clients/attendance_submit.js` 和 `schemas/attendance.py` 中实现，沿用相同的分层与会话工作线程。待办等能力可继续按此结构扩展。

可信后端通过会话命令提交业务，自动使用数据库认证并保留原任务：

```python
result = session.call("user_info")
```

新增可重试的 GET 业务时，通过 `AuthenticatedRequestService.execute(operation)` 接收从仓库加载的 `SavedSession`，将其中的 Cookie / Token 交给客户端。`session.credentials()` 仅为可信后端提供本地快照副本，不能替代业务请求的数据库认证来源，也不要返回给模型、日志或全局缓存。

账号密码由本机环境变量或 `.env` 读取；Cookie / Token 加密保存到 MySQL，不输出到聊天、应用日志或认证状态文件。登录工具只返回状态与数量，员工工具返回业务数据。默认不启用浏览器跟踪或认证状态文件导出。

## 验证

```bash
python -m unittest discover -s tests -v
```

测试覆盖凭据提取边界、登录规则失败、会话隔离和清理。真实 OA 登录仍需在可访问 OA 的电脑上由用户完成验证。

单元/集成测试覆盖加密仓库、按用户隔离、过期版本删除、数据库错误码诊断、401 后原任务续跑、MFA 等待、任务取消和页面重跑不重复执行。另已通过 Chrome 模拟登录及真实 Playwright 本机 HTTP 检查。当前配置的真实 MySQL 已验证连接、建库建表和加密读写，临时诊断记录已删除；MySQL 仓库单测仍使用隔离的模拟连接。新旧两个入口均有 Streamlit 界面测试。

数据库故障会返回经过脱敏的具体原因：例如 1049 表示 `MYSQL_DATABASE` 指定的数据库不存在，1045 表示账号认证被拒绝，2003 表示连接失败，1142 表示表访问权限不足。密钥无法解密现有会话时，也会给出独立提示，不展示原始连接异常、密码或认证数据。

已安装系统 Chrome 时，还可以运行浏览器集成检查（全部页面请求均由本地模拟响应拦截，不访问真实 OA）：

```bash
python tests/integration/browser_smoke.py
python tests/integration/auto_login_smoke.py
python tests/integration/employee_api_smoke.py
```

前两个检查使用隔离的内存仓库替身，第三个用本机 HTTP 服务和真实 Playwright 请求，验证仓库 Cookie/Token 更新后原调用自动成功。真实 MySQL 检查使用 `tests/integration/mysql_session_smoke.py`：针对独立临时数据库（测试账号 `oa_test`、测试密码 `local-test-only`）设置 `MYSQL_SMOKE_PORT` 后运行，验证真实 SQL 加密读写和 Chrome 登录后入库。测试不读取本机真实账号配置，不提交真实 OA 登录请求。模型在线调用尚未测试。
