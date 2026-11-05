#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开机自启 —— 只写当前用户的 HKCU\\...\\Run，不碰系统级、不需要管理员权限。

为什么用 HKCU Run，而不是计划任务 / Windows 服务：
  · 计划任务要走 schtasks，杀软常拦，参数也啰嗦；
  · 注册成服务要管理员权限，还会让"点退出"失去意义；
  · HKCU 是用户级、免提权、随用户登录拉起，正好匹配"托盘常驻"的定位；
  · 关掉只需要删一个值，不残留任何东西。

注册表位置：
  HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
  值名 ServiceHub，类型 REG_SZ，内容形如
      "C:\\...\\pythonw.exe" "D:\\Tools\\服务台\\ServiceHub.py" --no-open

为什么用 pythonw.exe：python.exe 会带一个黑框，开机弹一下很难看。
为什么带 --no-open：开机时只让托盘出现，不要抢着把浏览器弹出来。
"""

import os
import sys

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
VALUE_NAME = 'ServiceHub'

try:
    import winreg
except Exception:       # 非 Windows 平台
    winreg = None

SUPPORTED = winreg is not None


def _pythonw():
    """优先返回同目录下的 pythonw.exe；找不到就退回当前解释器。"""
    exe = sys.executable or ''
    folder, name = os.path.split(exe)
    if name.lower() in ('python.exe', 'pythonw.exe'):
        cand = os.path.join(folder, 'pythonw.exe')
        if os.path.isfile(cand):
            return cand
    return exe


def command(appdir):
    """拼出开机要执行的命令行（带引号，路径含空格/中文都不会断）。"""
    script = os.path.join(appdir, 'ServiceHub.py')
    return '"%s" "%s" --no-open' % (_pythonw(), script)


def current():
    """读注册表里已登记的命令；没登记或读不到则返回空串。"""
    if winreg is None:
        return ''
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
    except OSError:
        return ''
    try:
        try:
            value = winreg.QueryValueEx(key, VALUE_NAME)[0]
        except OSError:
            return ''
        return str(value or '').strip()
    finally:
        winreg.CloseKey(key)


def is_enabled():
    return bool(current())


def enable(appdir):
    """登记开机自启。返回 (成功?, 给用户看的一句话)。"""
    if winreg is None:
        return False, '只有 Windows 才支持写注册表自启'
    cmd = command(appdir)
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    except OSError as exc:
        return False, '打不开注册表启动项：%s' % exc
    try:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as exc:
        return False, '写入注册表失败：%s' % exc
    finally:
        winreg.CloseKey(key)
    return True, '已设为开机自启：下次登录 Windows 时托盘自动出现（不会自动弹浏览器）'


def disable():
    """取消开机自启。返回 (成功?, 给用户看的一句话)。"""
    if winreg is None:
        return False, '只有 Windows 才支持写注册表自启'
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
    except OSError:
        return True, '开机自启本来就是关着的'
    try:
        winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True, '开机自启本来就是关着的'
    except OSError as exc:
        return False, '删除注册表值失败：%s' % exc
    finally:
        winreg.CloseKey(key)
    return True, '已取消开机自启'


def status(appdir=None):
    """给界面用的完整状态。

    stale 表示"注册表里登记的命令"和"当前程序路径"对不上——
    典型场景是把 D:\\Tools\\服务台 整个目录挪走了，这时自启会指向旧路径，
    界面上要提示用户重新勾一次（重新勾就等于重写命令）。
    """
    cmd = current()
    want = command(appdir) if appdir else ''
    return {
        'supported': SUPPORTED,
        'enabled': bool(cmd),
        'command': cmd,
        'expected': want,
        'stale': bool(cmd and want and cmd != want),
    }
