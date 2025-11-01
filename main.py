#!/usr/bin/env python3
"""
每小时截一次 Chrome 窗口并上传到 /rest/foex/uploadImg
python chrome_hourly.py  即可启动，Ctrl-C 退出
"""
import base64
import io
import json
import logging
import sys
import time
import traceback
from datetime import datetime
import pyautogui

import pygetwindow as gw

import requests
from PIL import Image
import pywinctl as pwc  # 跨平台窗口管理
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os
import subprocess
from pathlib import Path


# ...existing code...
IMG_DIR = "img"  # 图片保存的根目录

# 配置：优先从环境变量读取，支持项目根目录的 .env（若安装 python-dotenv 则优先使用）
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_env_file(path: str):
    if not os.path.exists(path):
        return
    # 优先尝试 python-dotenv（若安装）
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except Exception:
        pass
    # 简单的 .env 解析回退方案
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    except Exception:
        pass

# 尝试加载项目根目录下的 .env
_load_env_file(os.path.join(REPO_DIR, ".env"))

# 从环境变量读取，若未设置则使用原来的默认值或占位符
UPLOAD_URL = os.environ.get("UPLOAD_URL", "/uploadImg")
WXPUSHER_URL = os.environ.get("WXPUSHER_URL", "/wxPusher")
TOKEN = os.environ.get("TOKEN", "")  # 强烈建议在 .env 或系统环境中设置 TOKEN

IMG_DIR = "img"  # 图片保存的根目录
# ========= 配置区 =========
# 添加Git相关配置
GIT_REPO_PATH = REPO_DIR  # 替换为你的Git仓库本地路径
GITHUB_REPO = "https://github.com/Dave828/mt4snap.git"  # 替换为你的GitHub仓库地址

MAX_RETRY = 3
TIMEOUT = 1500
LOG_FMT = "%(asctime)s | %(levelname)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT, handlers=[
    logging.FileHandler("upload.log", encoding="utf-8"),
    logging.StreamHandler(sys.stdout)
])
# ===========================

def find_chrome_window():
    """返回第一个标题包含 Chrome 的窗口对象"""
    for w in pwc.getAllWindows():
        print( w.title)
        if "chrome" in w.title.lower():
            return w
    return None
def find_mt4_window():
    """返回第一个标题包含 MT4 的窗口对象"""
    mt4_keywords = ["215468", "metatrader 4", "MetaTrader 4"]    
    for w in gw.getAllTitles():
        title = w.lower()
        #print( w.title)
        if any(k in title for k in mt4_keywords):
            print( "-->"+w)
            return gw.getWindowsWithTitle(w)[0]
    return None


def screenshot_window(win):
    """传入窗口对象，返回 Pillow.Image。
    优先尝试 pywinctl 的窗口级截图，回退到 restore+activate 后用 pyautogui 截屏。
    """
    # 1) 尝试 pywinctl 提供的窗口截图（有的平台/版本可能有效）
    try:
        if hasattr(win, "getScreenshot"):
            raw = win.getScreenshot()
            if raw:
                # 如果 getScreenshot 返回的是 PIL.Image 直接使用；否则尝试按常见结构转换
                if isinstance(raw, Image.Image):
                    return raw.convert("RGB")
                # 某些版本返回带 .bgra/size 等属性的对象
                if hasattr(raw, "bgra") and hasattr(raw, "size"):
                    return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX").convert("RGB")
    except Exception:
        logging.debug("win.getScreenshot() 失败，使用回退方法: " + traceback.format_exc())

    # 2) 回退：确保窗口非最小并置前，然后用 pyautogui 截取对应区域
    try:
        if hasattr(win, "isMinimized") and win.isMinimized():
            try:
                win.restore()
            except Exception:
                pass
        # 尝试激活/置前
        for fn in ("activate", "set_foreground", "bringToFront", "bring_to_front"):
            if hasattr(win, fn):
                try:
                    getattr(win, fn)()
                except Exception:
                    pass
        # 给系统一些时间把窗口置前
        time.sleep(0.35)
    except Exception:
        logging.debug("尝试置前窗口失败: " + traceback.format_exc())

    # 获取坐标并截图（注意 DPI 缩放可能导致坐标偏差）
    left, top, width, height = win.left, win.top, win.width, win.height
    img = pyautogui.screenshot(region=(left, top, width, height))
    return img.convert("RGB")

def compress_image(img: Image.Image, quality=85) -> bytes:
    """压缩成 JPEG，返回字节"""
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()
# 新增保存图片函数
def save_local_image(jpeg_bytes: bytes) -> str:
    """保存图片到本地按日期组织的目录，返回保存路径"""
    # 确保根目录存在
    if not os.path.exists(IMG_DIR):
        os.makedirs(IMG_DIR)
    
    # 按日期创建子目录
    today = datetime.now().strftime("%Y-%m-%d")
    day_dir = os.path.join(IMG_DIR, today)
    if not os.path.exists(day_dir):
        os.makedirs(day_dir)
    
    # 生成文件名和完整路径
    fname = datetime.now().strftime("%H%M%S.jpg")
    fpath = os.path.join(day_dir, fname)
    
    # 保存图片
    with open(fpath, "wb") as f:
        f.write(jpeg_bytes)
    
    logging.info(f"图片已保存到: {fpath}")
    return fpath
def upload_image(binary_jpeg: bytes, token: str) -> str:
    """上传图片，成功返回图片的 URL（字符串）。失败抛出异常。"""
    fname = datetime.now().strftime("screenshot_%Y-%m-%d_%H%M%S.jpg")
    files = {"file": (fname, binary_jpeg, "image/jpeg")}
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "pysnap/1.0"
    }
    backoff = 1
    for i in range(1, MAX_RETRY + 1):
        try:
            r = requests.post(UPLOAD_URL, files=files, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            try:
                j = r.json()
                logging.info(f"上传成功, 接口返回: {json.dumps(j, ensure_ascii=False)}")
            except ValueError:
                body = r.text or "<empty>"
                logging.warning(f"上传返回非 JSON 响应，status={r.status_code}, body={body[:200]!r}")
                raise RuntimeError("上传返回非 JSON 数据")

            # 常见响应结构兼容：data.url、url、data.key（不构建外链）
            url = None
            if isinstance(j, dict):
                data = j.get("data") or {}
                if isinstance(data, dict):
                    url = data.get("url")
                if not url:
                    url = j.get("url")

            if url:
                return url
            # 如果接口用 code/msg 标识成功但没有 url，给出明确错误
            logging.warning(f"上传成功但未找到 url 字段，full response: {j}")
            raise RuntimeError("上传成功但未返回图片 URL")
        except requests.exceptions.RequestException as e:
            logging.warning(f"第 {i} 次上传失败: {e}")
        except Exception as e:
            logging.warning(f"第 {i} 次上传异常: {e}")

        if i == MAX_RETRY:
            break
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

    raise RuntimeError("上传多次重试失败")
# 添加Git操作相关函数
def git_commit_and_push(file_path: str) -> bool:
    """
    将新保存的截图提交并推送到GitHub
    :param file_path: 要提交的文件路径
    :return: 是否成功
    """
    try:
        # 切换到仓库目录
        os.chdir(GIT_REPO_PATH)
        
        # 获取相对路径
        rel_path = os.path.relpath(file_path, GIT_REPO_PATH)
        
        # 执行git命令
        commands = [
            ['git', 'add', rel_path],
            ['git', 'commit', '-m', f'Add screenshot: {rel_path}'],
            ['git', 'push', 'origin', 'main']  # 假设使用main分支，根据需要修改
        ]
        
        for cmd in commands:
            result = subprocess.run(cmd, 
                                 capture_output=True, 
                                 text=True)
            if result.returncode != 0:
                logging.error(f"Git命令失败 {' '.join(cmd)}: {result.stderr}")
                return False
            
        logging.info(f"成功提交并推送文件到GitHub: {rel_path}")
        return True
        
    except Exception as e:
        logging.error(f"Git操作异常: {str(e)}")
        return False

# 新增：发送到 /wxPusher
def send_wx_pusher(img_url: str, token: str, uid: str = "UID_ofwaXLRDnlr8Rfg9hlGvhhq8M5NB") -> bool:
    """调用后台 /wxPusher，summary 为当前北京时间的描述，content 为图片 url。返回是否成功。"""
    try:
        tz = ZoneInfo("Asia/Shanghai")
        summary = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S") + " 的截图"
        data = {"uid": uid, "summary": summary, "content": summary+"如下<br><a href='"+img_url+"'>"+img_url+"</a>", "contentType": 2}
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "pysnap/1.0",
            "Content-Type": "application/json;charset=UTF-8"
        }
        logging.info( data )
        r = requests.post(WXPUSHER_URL,  json=data, headers=headers, timeout=15)
        r.raise_for_status()
        # 尝试解析返回 JSON 用于判断（若你的服务返回固定结构可按需调整）
        try:
            j = r.json()
            logging.info(f"wxPusher 返回: {json.dumps(j, ensure_ascii=False)}")
        except ValueError:
            logging.info(f"wxPusher 返回非 JSON 响应，status={r.status_code}")
        return True
    except Exception:
        logging.error("发送 wxPusher 失败: " + traceback.format_exc())
        return False
def job_once():
    """单次任务：截图 -> 上传"""
    win = find_mt4_window()
    if not win:
        logging.warning("未找到窗口，跳过本次")
        return
    logging.info(f"捕获窗口: {win.title}")
    img = screenshot_window(win)
    jpeg = compress_image(img)
    logging.info(f"截图压缩后大小: {len(jpeg)/1024:.1f} KB")
       # 保存到本地
    local_path = save_local_image(jpeg)
    # 提交到GitHub
    # if git_commit_and_push(local_path):
    #     logging.info("已提交截图到GitHub")
    # else:
    #     logging.warning("提交截图到GitHub失败")
    try:
        url = upload_image(jpeg, TOKEN)  # 返回图片 URL 字符串
        logging.info(f"上传成功, url: {url}")
        # 上传成功后调用推送接口
        ok = send_wx_pusher(url, TOKEN, uid="UID_ofwaXLRDnlr8Rfg9hlGvhhq8M5NB")
        if ok:
            logging.info("已发送推送到 /wxPusher")
        else:
            logging.warning("推送到 /wxPusher 失败")
        
    except Exception:
        logging.error("上传失败: " + traceback.format_exc())
def main():
    tz = ZoneInfo("Asia/Shanghai")
    logging.info("定时截图启动：北京时间每天 08:00-22:00 每小时执行一次，周末暂停，Ctrl-C 退出")
    while True:
        try:
            now = datetime.now(tz)

            # 周末(周六=5, 周日=6)暂停截图
            if now.weekday() >= 5:
                logging.info(f"当前为周末 ({now.strftime('%A')})，暂停截图，等待到下周一 08:00 开始")
            else:
                if 8 <= now.hour <= 22:
                    job_once()
                else:
                    logging.info(f"当前北京时间 {now.strftime('%Y-%m-%d %H:%M:%S')}，不在执行时间段（08-22），跳过本次")

        except KeyboardInterrupt:
            logging.info("用户中断，退出")
            break
        except Exception:
            logging.error("任务异常: " + traceback.format_exc())

        # 计算下次运行时间
        now = datetime.now(tz)
        if now.weekday() >= 5:
            # 周末：计算到下周一 08:00
            days_ahead = (7 - now.weekday())  # 周六->2, 周日->1
            next_run = (now + timedelta(days=days_ahead)).replace(hour=8, minute=0, second=0, microsecond=0)
        else:
            next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            if next_hour.hour < 8:
                next_run = next_hour.replace(hour=8, minute=0, second=0, microsecond=0)
            elif next_hour.hour > 22:
                # 明天 08:00 （若明天为周末会在下一循环中被调整）
                next_run = (next_hour + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
            else:
                next_run = next_hour

        sleep_sec = (next_run - datetime.now(tz)).total_seconds()
        if sleep_sec > 0:
            logging.info(f"下次执行（北京时间）: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
            time.sleep(sleep_sec)
if __name__ == "__main__":
    main()