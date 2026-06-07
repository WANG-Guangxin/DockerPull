## Docker Pull 🗽

一个基于 GitHub Actions 的 Docker 镜像拉取与单文件下载机器人。

### 使用方法

在本仓库中提交一个 Issue，Issue 标题就是你要执行的命令。

### 支持的命令

**1. Docker 镜像下载**

```bash
docker pull <image>
```

示例：

```bash
docker pull nginx:latest
```

**2. Wget 文件下载**

```bash
wget [flags] <URL>
```

示例：

```bash
wget https://example.com/file.zip
wget -q -c --header "Accept: application/octet-stream" https://example.com/file.zip
```

**3. Curl 文件下载**

```bash
curl [flags] <URL>
```

示例：

```bash
curl https://example.com/file.zip
curl -sSL https://resource.fit2cloud.com/1panel/package/v2/quick_start.sh
curl -H "Accept: application/json" --retry 5 https://example.com/file.json
```

![](./image/000.png)

等待几分钟后，机器人会在 Issue 中回复下载链接。

![](./image/001.png)

点击回复中的链接即可下载文件。

如果下载的是 Docker 镜像归档文件，可以在本地执行：

```bash
docker load < image.tar.gz
```

### 下载参数支持

机器人支持很多常见的 `wget` 和 `curl` 单文件下载参数，包括静默模式、重定向、重试控制、自定义请求头、referer、user-agent、超时设置、content disposition，以及断点续传相关参数。

支持的命令形式示例：

```bash
curl -sSL <URL>
curl -H "Authorization: Bearer public-token" --retry 5 --max-time 600 <URL>
wget -q -c --content-disposition --header "Accept: */*" <URL>
```

### 使用限制

为了保证安全性和行为可预测，当前 workflow 只支持“下载单个文件”的命令。

以下行为暂不支持：

- 使用 `-o`、`-O` 或 `--output-document` 指定自定义输出路径
- 递归下载或镜像下载
- 上传、表单提交、请求体、自定义请求方法等非下载行为
- 一个 Issue 中包含多个下载 URL

如果目标文件超过 GitHub Actions runner 或云端上传步骤可承受的容量，workflow 会提前终止并回帖提示错误。

如果这个项目对你有帮助，欢迎点一个 Star。

