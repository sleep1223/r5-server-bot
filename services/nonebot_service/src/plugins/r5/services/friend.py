from nonebot import logger, on_notice, on_request
from nonebot.adapters.onebot.v11 import Bot, FriendAddNoticeEvent, FriendRequestEvent

from .help import get_help_message

auto_accept_friend = on_request(priority=5, block=False)
friend_added = on_notice(priority=5, block=False)


@auto_accept_friend.handle()
async def handle_friend_request(bot: Bot, event: FriendRequestEvent) -> None:
    await bot.set_friend_add_request(flag=event.flag, approve=True)
    logger.info(f"已自动接受好友申请: {event.user_id}")


@friend_added.handle()
async def handle_friend_added(bot: Bot, event: FriendAddNoticeEvent) -> None:
    welcome = "👋 你好！我是 R5 Bot，感谢添加好友！\n\n" + get_help_message()
    await bot.send_private_msg(user_id=event.user_id, message=welcome)
    logger.info(f"已向新好友发送欢迎消息: {event.user_id}")
