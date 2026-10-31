"""系统托盘（可选）。

托盘不是必需品：网页 + 桌面快捷方式已经够用。这里做成"能用就更好，用不了不影响"，
pystray 缺失或初始化失败时直接降级为无托盘模式，只在日志里留一句。
"""
import os
import threading


def _make_icon_image():
    """用 Pillow 现画一个图标：蓝色圆角底 + 白色电源符号，不依赖外部 ico 文件。"""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=14, fill=(22, 119, 255, 255))
    # 电源符号：一段开口圆弧 + 一条竖线
    draw.arc([16, 13, 48, 45], start=300, end=240, fill=(255, 255, 255, 255), width=5)
    draw.line([32, 28, 32, 48], fill=(255, 255, 255, 255), width=5)
    return img


def start_tray(ctx, hub_url, on_open, on_start_all, on_stop_all, on_exit):
    """返回托盘对象；失败返回 None，调用方继续无托盘运行。

    为什么要返回对象：退出时要显式 stop()，否则托盘的消息循环线程可能挂着不让进程结束，
    留下一具"端口已关、进程还在"的僵尸实例（实测踩过）。
    """
    try:
        import pystray
    except Exception as exc:
        ctx.logger.warn('托盘不可用（缺少 pystray：%s），改用网页入口' % exc)
        return None

    def open_panel(icon=None, item=None):
        try:
            on_open()
        except Exception as exc:
            ctx.logger.error('打开面板失败：%r' % exc)

    def start_all(icon=None, item=None):
        try:
            result = on_start_all()
            ctx.logger.ok('托盘触发全部启动：%s' % result.get('message'))
        except Exception as exc:
            ctx.logger.error('全部启动异常：%r' % exc)

    def stop_all(icon=None, item=None):
        try:
            result = on_stop_all()
            ctx.logger.ok('托盘触发全部停止：%s' % result.get('message'))
        except Exception as exc:
            ctx.logger.error('全部停止异常：%r' % exc)

    def quit_hub(icon=None, item=None):
        try:
            icon.stop()
        except Exception:
            pass
        on_exit()

    menu = pystray.Menu(
        pystray.MenuItem('打开服务台面板', open_panel, default=True),
        pystray.MenuItem('全部启动', start_all),
        pystray.MenuItem('全部停止', stop_all),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('退出服务台', quit_hub),
    )
    try:
        icon = pystray.Icon('ServiceHub', _make_icon_image(), '服务台 · %s' % hub_url, menu)
    except Exception as exc:
        ctx.logger.warn('托盘图标创建失败：%r' % exc)
        return None

    try:
        icon.run_detached()
        ctx.logger.ok('托盘图标已就绪')
        return icon
    except Exception as exc:
        ctx.logger.warn('托盘启动失败：%r' % exc)
        try:
            threading.Thread(target=icon.run, name='tray', daemon=True).start()
            ctx.logger.ok('托盘图标已就绪（线程模式）')
            return icon
        except Exception as exc2:
            ctx.logger.warn('托盘线程也起不来：%r' % exc2)
            return None
