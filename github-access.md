---
name: github-access
description: 打通 GitHub 访问链路：先判网络（网页 HTTPS 可能被墙，但 SSH 22/443 通常通，api/raw 也可能通）、复用或生成 SSH 密钥、配置 22/443 双通道、设置仓库级提交身份与 noreply 邮箱、用 --recurse-submodules 克隆私有仓库、推送权限实测，以及 404 / Permission denied / 子模块为空 的故障对照。适用于新机器首次获取 GitHub 私有项目，或 clone/push 报错时。
whenToUse: 用户要从 GitHub 拉私有仓库、配置 SSH 认证、配置 git 提交身份，或遇到 Repository not found / Permission denied (publickey) / 子模块目录为空 / 网页打不开但想 Clone 时。
---

# GitHub 访问与认证（SSH 优先）

固定顺序：**网络判定 → 密钥 → 身份 → 克隆 → 推送实测 → 排错**。
不要跳过推送实测——能 clone ≠ 能 push，权限、只读 key、组织策略都只在推的时候暴露。

---

## 0. 先分清两个"没有权限"

| 现象 | 真实含义 | 处置 |
|---|---|---|
| 网页打开仓库 **404** | GitHub 对无权限者返回 404（不是"无权限"提示） | 查 GitHub 通知里的 pending invite；没有就让仓库主重发 |
| clone 报 **Repository not found** | 同上：邀请没接受，或没发到该账号 | 同上；**不要**怀疑仓库不存在、不要换 URL 重试 |
| clone 报 **Permission denied (publickey)** | 密钥没加到该账号 / 加错账号 / ssh 没选对 key | 核对指纹与 GitHub Settings → SSH keys 里的是否一致 |

私有仓库的这两种错误原因完全相同：**都是授权问题，不是网络问题**。先排除授权再查网络。

---

## 1. 网络判定：先测，再决定要不要"加速"

国内环境常见非对称封锁：**网页路径挂、SSH 路径通**。逐个探，别一概而论。

```bash
# 网页与 API
curl -4 -sS -o /dev/null -w "github.com/ %{http_code} %{time_total}s\n" https://github.com/ --max-time 20
curl -4 -sS -o /dev/null -w "api %{http_code}\n"        https://api.github.com/ --max-time 20
curl -4 -sS -o /dev/null -w "raw %{http_code}\n"        https://raw.githubusercontent.com/ --max-time 20

# SSH 两种端口（能拿到 "Permission denied (publickey)" 就说明传输层是通的）
ssh -o BatchMode=yes -o ConnectTimeout=10 -T git@github.com             # 22
ssh -o BatchMode=yes -o ConnectTimeout=10 -p 443 -T git@ssh.github.com  # 443
```

判定规则：

- **能握手但提示 `Permission denied (publickey)` = 传输通、只是缺正确密钥** → 直接配密钥，不需要代理。
- 只有 `github.com/xxx` 页面超时、`api` 与 `raw` 正常 → 属于网页被墙，**clone 走 SSH 完全不受影响**。
- `git ls-remote` 对**公开仓库**都超时，才需要考虑代理/镜像。

**镜像的边界**（很重要）：

| 用途 | 可以 | 不可以 |
|---|---|---|
| 下载 release / raw 文件 | `https://ghproxy.net/https://github.com/...`、`gh-proxy.com`、`gitclone.com` | — |
| clone **私有**仓库 | — | ❌ 任何第三方镜像。等于把凭据交给陌生人 |

`ssh.github.com:443` 只是 SSH over 443，不是 HTTPS 代理；用 `curl https://ssh.github.com` 测它会得到 TLS 错误，那是正常的，**用 ssh 命令测**。

---

## 2. SSH 密钥

### 2.1 复用已有密钥（双系统 / 从旧机器搬）

Windows 的 `C:\Users\<用户>\.ssh\` 在 Linux 上可直接读——但 **C: 盘的设备号会变，必须按 UUID 找**，不能写死 `/dev/nvmeXnYpZ`。

```bash
# 1) 按 UUID/标签定位 Windows 系统盘（本机 C: 的 UUID 是 9E66EC5F66EC39A3）
lsblk -f -o NAME,FSTYPE,LABEL,UUID,SIZE,MOUNTPOINT | grep -i -E 'ntfs|Windows'

# 2) 只读挂载（不需要 root；挂载点自动生成）
udisksctl mount -b /dev/nvme0n1p3 -o ro
W=$(findmnt -rn -S /dev/nvme0n1p3 -o TARGET)

# 3) 找密钥（多个用户目录都看一遍）
ls -la "$W"/Users/*/.ssh/

# 4) 复制并收紧权限（只复制，绝不 cat 私钥）
cp "$W/Users/<用户>/.ssh/id_ed25519" ~/.ssh/id_ed25519_win
chmod 600 ~/.ssh/id_ed25519_win

# 5) 从私钥推导公钥 + 看指纹（内容不落屏，只看指纹）
ssh-keygen -l -f ~/.ssh/id_ed25519_win
ssh-keygen -y -P "" -f ~/.ssh/id_ed25519_win    # 能输出 = 无口令；若报错 = 带口令，需要 agent
```

要点：

- 私钥**不要**在对话里贴、不要打印、不要发给任何 Agent。
- Windows 上常常只有 `id_ed25519` 没有 `.pub`，用 `ssh-keygen -y -f` 推导即可。
- 带口令的私钥在非交互 shell 里用不了，需要 `ssh-agent` + `ssh-add`（会提示输入口令，得由用户操作）。

### 2.2 新生成

```bash
ssh-keygen -t ed25519 -C "<GitHub 邮箱>" -f ~/.ssh/id_ed25519 -N ""   # 口令留空便于自动化
chmod 600 ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub                                              # 只输出这一行
```

### 2.3 把公钥加到 GitHub

路径：头像 → Settings → SSH and GPG keys → New SSH key。
**若本机网页被墙**：用手机 GitHub App / 手机浏览器 / 另一台能上网的电脑做这一步，只需要公钥那一行。

> 安全红线：任何时候只提供 `*.pub`。谁要私钥都不给，包括 Agent。

---

## 3. `~/.ssh/config`（22 优先，443 兜底）

```sshconfig
Host github.com
  HostName github.com
  Port 22
  User git
  IdentityFile ~/.ssh/id_ed25519_win
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ServerAliveInterval 30
  ServerAliveCountMax 3

# 22 端口不稳时：ssh -T github-443，或在 git 里用 git@github-443:owner/repo.git
Host github-443
  HostName ssh.github.com
  Port 443
  User git
  IdentityFile ~/.ssh/id_ed25519_win
  IdentitiesOnly yes
```

**踩过的坑**：不要把 `github.com` 同时写进两个 Host 块。OpenSSH 对每个关键字只取**首次出现的值**，第二块里改 `HostName`/`Port` 不会生效，结果会连到 `github.com:443` 并报 `Connection closed by remote host`——看起来像网络故障，其实是配置串了。第二个块只写 `github-443`。

`IdentitiesOnly yes` 必须加：否则 agent 里其它 key 会先被尝试，容易触发 GitHub 的失败计数或选错账号。

---

## 4. 提交身份（仓库级，不用 --global）

```bash
cd <仓库根>
git config user.name  "显示名"
git config user.email "<数字ID>+<用户名>@users.noreply.github.com"
```

- 数字 ID：`curl -s https://api.github.com/users/<用户名> | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"`
- noreply 邮箱既保护隐私，GitHub 也能正确关联提交与贡献图。
- 仓库级配置优先于全局；**不要**为单个项目污染全局身份。
- 校验：`git config user.name` / `git config user.email` 不为空、不是占位符（如 `hermes@example.com`）。

---

## 5. 克隆（含子模块）

```bash
git clone --recurse-submodules git@github.com:<owner>/<repo>.git
cd <repo>
```

**`--recurse-submodules` 不能省**：子模块是独立私有仓库，普通 clone 只会得到空目录，看起来像"代码丢了"。

补救：

```bash
git submodule update --init --recursive
git submodule status        # 首字符：空格=正常，'-'=没拉，'+'=提交与父仓库记录不一致
```

子模块最常见的两个坑：

1. **Repository not found** → 子模块仓库的邀请没接受（它和主仓库是两套授权）。
2. **detached HEAD** → `git -C <sub> rev-parse --abbrev-ref HEAD` 输出 `HEAD`；修复：`git -C <sub> switch <分支>`（无本地分支时 `switch -c <分支> --track origin/<分支>`）。
   在 detached HEAD 上提交，代码不属于任何分支，一推就丢。

---

## 6. 验证清单（缺一不可）

```bash
# 1) 认证
ssh -T git@github.com
#    期望：Hi <用户名>! You've successfully authenticated...
#    注意：成功时退出码也是 1（GitHub 不提供 shell），不要被 rc=1 误导

# 2) 仓库可达（比 clone 快，先测这个）
GIT_SSH_COMMAND="ssh -o BatchMode=yes" git ls-remote git@github.com:<owner>/<repo>.git HEAD

# 3) 推送权限实测（唯一能证明 Write 的方法）
git switch -c test/envcheck-<名字>
echo "env check" > ENVCHECK.txt && git add ENVCHECK.txt
git commit -m "chore: 环境连通性测试"
git push -u origin HEAD
#    期望：remote 提示 Create a pull request for ...

# 4) 立刻清理
git push origin --delete test/envcheck-<名字>
git switch main && git branch -D test/envcheck-<名字> && rm -f ENVCHECK.txt
```

子模块安全网（多仓库协作必配）：

```bash
git config push.recurseSubmodules check   # 子模块指针没推时拒绝推父仓库
git config alias.spull "pull --recurse-submodules"
git config alias.sst   "submodule status --recursive"
```

> 装 pre-push 钩子前先看清楚它保护哪些分支。有的项目把子模块的长期分支（如 `feat/assignment-routing`）也列进受保护名单，导致"规程说可以直接推、钩子却拦住"的矛盾——遇到就先跟仓库主确认预期流程，别自己加 `--no-verify`。

---

## 7. 故障对照

| 症状 | 最可能原因 | 处置 |
|---|---|---|
| `Permission denied (publickey)` | 公钥没加到当前账号 | 核对 `ssh-keygen -l -f ~/.ssh/xxx` 指纹与 GitHub 上的是否一致 |
| `Connection closed by ... port 443` | config 里 Host 重复，SSH 连到了 443 | 见第 3 节，Host 块只写 `github-443` |
| clone `Repository not found` | 私有仓库无权限/邀请未接受 | 接受邀请；子模块要单独接受 |
| `could not read Username for 'https://github.com'` | 用了 HTTPS 又没配凭据 | 改 SSH；或配 PAT（不要贴进对话） |
| `Host key verification failed` | known_hosts 缺记录 | `ssh-keyscan github.com >> ~/.ssh/known_hosts`（或首次输 yes） |
| `ssh -T` 返回 1 但显示 Hi | 正常 | GitHub 不提供 shell，成功也是 rc=1 |
| 子模块目录为空 | clone 没带 `--recurse-submodules` | `git submodule update --init --recursive` |
| 子模块分支名输出 `HEAD` | detached HEAD | 切回开发分支 |
| 网页打不开但 SSH 通 | 非对称封锁 | 继续用 SSH，不需要代理 |
| 大文件下载超时（deb/release） | 走 GitHub 直链 | 用 ghproxy 前缀代理**公开**资源 |
| 克隆成功但 `git sst` 有 `+` | 本地子模块提交与父仓库指针不一致 | 先确认该提交是否已推到远端，再 bump 指针 |

---

## 8. 与相邻文档的关系

| 想知道什么 | 去哪 |
|---|---|
| 本机 SSH 客户端怎么用（config、隧道、传文件、排错） | 工作区 `本机SSH使用指南.md` |
| 项目服务器（106.55.27.144）怎么连 | 仓库 `文档/04-开发交接/项目级SSH连接说明.md` + `运维脚本/server-ssh.ps1` |
| 子模块提交/推送顺序 | 仓库 `文档/05-协作规范/子模块协作规程.md` |
| 全局 skill 目录 | `~/.dsh/skills/` |

**已知仓库不一致**：`运维脚本/Setup-DevEnv.ps1` 与 `文档/05-协作规范/` 可能只在 `chore/collaboration-setup` 分支上，`main` 上没有。按文档在 `main` 执行会找不到文件，此时用 `git show origin/chore/collaboration-setup:<路径>` 取内容，或按其 Linux 等价步骤手工执行。

---

## 9. 本机已知值（felix / Ubuntu 22.04）

| 项 | 值 |
|---|---|
| GitHub 账号 | `2-3-3-3`（数字 ID `202271377`） |
| 复用密钥 | `~/.ssh/id_ed25519_win`（源自 `Users/Hongfan Liu/.ssh/id_ed25519`，ED25519，无口令） |
| 备用密钥 | `~/.ssh/id_ed25519` |
| 网络 | 网页 HTTPS 超时；`github.com:22`、`ssh.github.com:443`、`api`、`raw` 可用 |
| 项目 | `/media/felix/Data/coding/wecom-cs-v2`，子模块 `tgo` @ `feat/assignment-routing` |
| 提交身份 | 仓库级 `2-3-3-3 <1315892280@qq.com>`（文档推荐改用 noreply：`202271377+2-3-3-3@users.noreply.github.com`） |
| Windows 盘 | C: UUID `9E66EC5F66EC39A3`，设备号不固定，按 UUID 挂载 |

**Windows / PowerShell 5.1 对照**（主力开发机）：

```powershell
ssh-keygen -t ed25519 -C "邮箱"          # 公钥在 C:\Users\<你>\.ssh\id_ed25519.pub
type $env:USERPROFILE\.ssh\id_ed25519.pub
ssh -T git@github.com
```

PS 5.1 传给 `ssh.exe` 的内嵌引号会被剥掉，不要拼超长 ssh 命令；含中文的 `.ps1` 必须存为 UTF-8 with BOM。
