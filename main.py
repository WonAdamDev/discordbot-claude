import os
import io
import base64
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import commands
import anthropic
from PIL import Image

# 환경변수 로드
load_dotenv()

# Anthropic 클라이언트 초기화 (비동기 클라이언트: 응답을 기다리는 동안 봇 이벤트 루프를 막지 않음)
anthropic_client = anthropic.AsyncAnthropic(
    api_key=os.getenv('ANTHROPIC_API_KEY')
)

CLAUDE_MODEL = "claude-opus-5-5"

# 호출할 때마다 게시물 전체를 보내므로 비용 안전장치
MAX_HISTORY_MESSAGES = 500  # 이보다 길면 첫 글 + 최근 글만 보냄
MAX_IMAGES = 20             # 최근 이미지 N장만 실제로 보내고, 나머지는 파일명만 표시
IMAGE_MAX_EDGE = 1280       # 이미지 긴 변을 이 크기로 축소 (1장당 약 1,200토큰)
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 이보다 큰 원본 이미지는 다운로드하지 않음

FORUM_ONLY_NOTICE = "💬 저는 포럼 채널의 게시물 안에서만 대화할 수 있어요. 포럼 게시물에서 `@멘션` 또는 `!ai [질문]`으로 불러주세요!"

SYSTEM_PROMPT = """당신은 디스코드 서버의 포럼 게시물(토론방)에서 호출되는 AI 어시스턴트 Claude입니다.
사용자 메시지에는 게시물 제목과 지금까지의 대화 내역이 "[이름] 내용" 형식으로 들어 있고, 맨 마지막에 당신을 호출한 사람과 요청이 있습니다.
- 호출한 사람의 요청에 집중해서 답하되, 게시물에서 나온 의견과 정보를 참고하세요.
- 대화 내역에서 [Claude(봇)]으로 표시된 글은 당신이 이전에 작성한 답변입니다.
- 특정 참여자의 의견을 언급할 때는 누구의 의견인지 이름을 밝혀주세요.
- 친근하고 유용하게 답변하고, 답변은 1800자를 넘지 않도록 해주세요.
- 필요하다면 코드 블록이나 마크다운을 사용해서 가독성을 높여주세요."""

# 디스코드 봇 설정 (기본 help 명령어 완전히 제거)
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.dm_messages = True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix='!', 
            intents=intents, 
            help_command=None  # 기본 help 명령어 비활성화
        )
        self.start_time = None

    async def setup_hook(self):
        """봇 초기 설정"""
        print("봇 설정 중...")

bot = MyBot()

# 봇이 준비되었을 때
@bot.event
async def on_ready():
    bot.start_time = discord.utils.utcnow()
    print(f'{bot.user} Claude AI 봇이 온라인입니다!')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="!ai 명령어"))

# 포럼 채널의 게시물인지 확인
def is_forum_post(channel) -> bool:
    return isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel)

# 대화록에 표시할 작성자 이름
def author_label(msg: discord.Message) -> str:
    if msg.author == bot.user:
        return "Claude(봇)"
    if msg.author.bot:
        return f"{msg.author.display_name}(봇)"
    return msg.author.display_name

def is_image(attachment: discord.Attachment) -> bool:
    return (attachment.content_type or "").startswith("image/")

# 이미지를 내려받아 축소한 뒤 Claude 이미지 블록으로 변환 (실패하면 None)
async def load_image_block(attachment: discord.Attachment):
    if attachment.size > MAX_IMAGE_BYTES:
        return None
    try:
        data = await attachment.read()

        def resize() -> str:
            img = Image.open(io.BytesIO(data))
            img.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))  # 비율 유지, 작은 이미지는 그대로
            if img.mode in ("RGBA", "LA", "P"):
                # 투명 배경은 흰색으로 채움
                img = img.convert("RGBA")
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.getchannel("A"))
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=85)
            return base64.standard_b64encode(buffer.getvalue()).decode()

        encoded = await asyncio.to_thread(resize)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": encoded},
        }
    except Exception as e:
        print(f"이미지 처리 실패 ({attachment.filename}): {e}")
        return None

# 게시물의 글을 오래된 순으로 가져옴 (너무 길면 첫 글 + 최근 글만)
async def collect_post_messages(thread: discord.Thread) -> list:
    messages = [m async for m in thread.history(limit=MAX_HISTORY_MESSAGES)]
    messages.reverse()

    # 포럼 게시물의 첫 글은 게시물(스레드)과 같은 ID - 잘렸으면 따로 가져와서 맨 앞에 붙임
    if messages and messages[0].id != thread.id:
        try:
            messages.insert(0, await thread.fetch_message(thread.id))
        except discord.HTTPException:
            pass  # 첫 글이 삭제된 경우

    return [m for m in messages if m.type in (discord.MessageType.default, discord.MessageType.reply)]

# 게시물 전체를 대화록 형태의 Claude 메시지 내용으로 만듦
async def build_forum_content(thread: discord.Thread, invoker, question: str) -> list:
    post_messages = await collect_post_messages(thread)

    # 최근 이미지 MAX_IMAGES장만 실제로 보냄
    images_to_send = [a for m in post_messages for a in m.attachments if is_image(a)][-MAX_IMAGES:]
    loaded = await asyncio.gather(*(load_image_block(a) for a in images_to_send))
    image_blocks = {a.id: block for a, block in zip(images_to_send, loaded) if block}

    content = [{"type": "text", "text": f"[게시물 제목] {thread.name}"}]
    for msg in post_messages:
        lines = [msg.clean_content] if msg.clean_content else []
        attached = []
        for attachment in msg.attachments:
            if attachment.id in image_blocks:
                attached.append(image_blocks[attachment.id])
            elif is_image(attachment):
                lines.append(f"[이미지: {attachment.filename}]")
            else:
                lines.append(f"[첨부: {attachment.filename}]")

        if not lines and not attached:
            continue  # 임베드만 있는 글 등
        text = "\n".join(lines) if lines else "(이미지)"
        # 글 하나당 블록 하나: 대화가 이어져도 앞부분이 그대로라 프롬프트 캐시가 재사용됨
        content.append({"type": "text", "text": f"[{author_label(msg)}] {text}"})
        content.extend(attached)

    # 대화록 끝에 캐시 지점 표시 - 5분 안에 같은 게시물에서 다시 부르면 이전 내역은 캐시 가격(90%+ 할인)으로 처리됨
    content[-1]["cache_control"] = {"type": "ephemeral"}

    request = question or "(요청 내용 없이 호출됨 - 지금까지의 토론을 보고 도움이 될 답변을 해주세요)"
    content.append({"type": "text", "text": f"---\n호출한 사람: {invoker.display_name}\n요청: {request}"})
    return content

# Claude AI에게 질문하기
async def ask_claude(content: list) -> str:
    try:
        # Opus 5.5는 thinking이 항상 켜져 있음 - effort로 생각하는 양(속도/비용)을 조절
        # thinking도 max_tokens에 포함되므로 넉넉하게 설정 (답변 길이는 시스템 프롬프트로 제한)
        # fallbacks="default": 안전 분류기가 거절하면 서버에서 다른 모델로 자동 재시도
        message = await anthropic_client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": content
                }
            ]
        )

        usage = message.usage
        print(f"토큰 사용: 입력 {usage.input_tokens}, 캐시 읽기 {usage.cache_read_input_tokens}, "
              f"캐시 쓰기 {usage.cache_creation_input_tokens}, 출력 {usage.output_tokens}")

        if message.stop_reason == "refusal":
            return "🙅 이 요청에는 답변할 수 없습니다. 질문을 바꿔서 다시 시도해주세요."

        # 응답 앞부분에 thinking 블록이 올 수 있으므로 text 블록만 모아서 반환
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            print(f"Claude 빈 응답: stop_reason={message.stop_reason}, request_id={message._request_id}")
            return "❓ AI가 답변을 생성하지 못했습니다. 다시 시도해주세요."

        return text

    except anthropic.AuthenticationError:
        return "❌ API 키가 유효하지 않습니다. 봇 관리자에게 문의하세요."
    except anthropic.RateLimitError:
        return "⏳ 요청이 너무 많습니다. 잠시 후 다시 시도해주세요."
    except anthropic.APIStatusError as e:
        print(f"Claude API 오류 ({e.status_code}): {e.message} [request_id={e.request_id}]")
        if e.status_code == 500:
            return "🔧 AI 서비스에 일시적인 문제가 있습니다. 잠시 후 다시 시도해주세요."
        else:
            return f"❓ AI 처리 중 오류가 발생했습니다. (상태 코드: {e.status_code})"
    except Exception as e:
        print(f"Claude API 오류: {e}")
        return "❓ AI 처리 중 오류가 발생했습니다. 다시 시도해주세요."

# 메시지 길이 제한 및 분할
def split_message(text: str, max_length: int = 1900) -> list:
    if len(text) <= max_length:
        return [text]
    
    messages = []
    current_message = ''
    lines = text.split('\n')
    
    for line in lines:
        if len(current_message + line + '\n') > max_length:
            if current_message:
                messages.append(current_message.strip())
                current_message = ''
            
            if len(line) > max_length:
                chunks = [line[i:i+max_length] for i in range(0, len(line), max_length)]
                for i, chunk in enumerate(chunks):
                    if i == len(chunks) - 1:
                        current_message = chunk + '\n'
                    else:
                        messages.append(chunk)
            else:
                current_message = line + '\n'
        else:
            current_message += line + '\n'
    
    if current_message.strip():
        messages.append(current_message.strip())
    
    return messages

# 포럼 게시물에서 AI 요청 처리
async def handle_forum_request(message, question: str):
    async with message.channel.typing():
        try:
            print(f"[{message.channel.name}] {message.author.name} 요청: {question}")

            content = await build_forum_content(message.channel, message.author, question)
            response = await ask_claude(content)

            message_parts = split_message(response)
            
            for i, part in enumerate(message_parts):
                if i == 0:
                    await message.reply(part)
                else:
                    await message.channel.send(part)
                
                if i < len(message_parts) - 1:
                    await asyncio.sleep(1)
            
            print(f"[{message.author.name}] 응답 완료")
            
        except Exception as e:
            print(f"메시지 처리 오류: {e}")
            await message.reply("❌ 처리 중 오류가 발생했습니다. 다시 시도해주세요.")

# 메시지 이벤트 처리
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    # DM에서는 AI 대화 대신 안내만 (!명령어는 위에서 처리됨)
    if isinstance(message.channel, discord.DMChannel):
        if not message.content.startswith('!'):
            await message.reply(FORUM_ONLY_NOTICE)
        return

    # 서버에서는 !ai 명령어나 봇 멘션(봇 글에 답장 포함)에만 반응
    is_mentioned = bot.user in message.mentions
    is_command = message.content.startswith('!ai ')

    if not is_mentioned and not is_command:
        return

    # 포럼 게시물이 아니면 안내만
    if not is_forum_post(message.channel):
        await message.reply(FORUM_ONLY_NOTICE)
        return

    # 요청 추출 (비어 있으면 토론 전체를 보고 답함)
    if is_command:
        question = message.content[4:]
    else:
        question = message.content.replace(f'<@{bot.user.id}>', '').replace(f'<@!{bot.user.id}>', '')

    await handle_forum_request(message, question.strip())

# 도움말 명령어 (help 충돌 완전 회피)
@bot.command(name='도움')
async def help_kr(ctx):
    embed = discord.Embed(
        title="🤖 Claude AI 디스코드 봇",
        description="Anthropic의 Claude AI를 디스코드에서 사용할 수 있습니다!",
        color=0x0099ff
    )
    
    embed.add_field(
        name="💬 AI와 대화하기",
        value="**포럼 채널의 게시물 안에서만** 사용할 수 있습니다.\n• `!ai [요청]` - AI 호출하기\n• `@봇멘션 [요청]` - 멘션으로 호출하기\n• 게시물의 전체 대화(이미지 포함)를 읽고 답합니다",
        inline=False
    )

    embed.add_field(
        name="📝 사용 예시",
        value="• `!ai 지금까지 나온 의견 정리해줘`\n• `!ai 철수 말이 맞아?`\n• `@봇멘션` - 요청 없이 부르면 토론을 보고 의견을 줍니다",
        inline=False
    )
    
    embed.add_field(
        name="🛠️ 명령어",
        value="• `!도움` - 이 도움말\n• `!상태` - 봇 상태 확인\n• `!헬프` - 영어 도움말",
        inline=False
    )
    
    embed.set_footer(text="Claude AI는 Anthropic에서 개발되었습니다")
    embed.timestamp = discord.utils.utcnow()
    
    await ctx.reply(embed=embed)

# 영어 도움말 (help 대신 헬프 사용)
@bot.command(name='헬프')
async def help_en(ctx):
    embed = discord.Embed(
        title="🤖 Claude AI Discord Bot",
        description="Use Anthropic's Claude AI in Discord!",
        color=0x0099ff
    )
    
    embed.add_field(
        name="💬 Chat with AI",
        value="Works **only inside forum channel posts**.\n• `!ai [request]` - Call AI\n• `@bot_mention [request]` - Call by mention\n• Reads the whole post conversation (including images)",
        inline=False
    )
    
    embed.add_field(
        name="📝 Examples",
        value="• `!ai Summarize the opinions so far`\n• `!ai Is Chulsoo right?`\n• `@bot_mention` - Without a request, it reads the discussion and weighs in",
        inline=False
    )
    
    embed.add_field(
        name="🛠️ Commands",
        value="• `!도움` - Korean help\n• `!상태` - Bot status\n• `!헬프` - This help",
        inline=False
    )
    
    embed.set_footer(text="Claude AI is developed by Anthropic")
    embed.timestamp = discord.utils.utcnow()
    
    await ctx.reply(embed=embed)

# 상태 확인 명령어
@bot.command(name='상태')
async def status_command(ctx):
    if bot.start_time:
        uptime_seconds = (discord.utils.utcnow() - bot.start_time).total_seconds()
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        seconds = int(uptime_seconds % 60)
        uptime_str = f"{hours}시간 {minutes}분 {seconds}초"
    else:
        uptime_str = "계산 중..."
    
    embed = discord.Embed(
        title="🤖 봇 상태",
        color=0x00ff00
    )
    
    embed.add_field(name="🟢 상태", value="온라인", inline=True)
    embed.add_field(name="⏱️ 실행 시간", value=uptime_str, inline=True)
    embed.add_field(name="🏃‍♂️ 지연시간", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="🤖 AI 모델", value="Claude Opus 5.5", inline=True)
    embed.add_field(name="📊 서버 수", value=f"{len(bot.guilds)}개", inline=True)
    embed.add_field(name="👥 사용자 수", value=f"{len(bot.users)}명", inline=True)
    
    embed.timestamp = discord.utils.utcnow()
    
    await ctx.reply(embed=embed)

# 오류 처리
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    
    print(f"명령어 오류: {error}")
    await ctx.reply("❌ 명령어 처리 중 오류가 발생했습니다.")

# 봇 실행
if __name__ == "__main__":
    discord_token = os.getenv('DISCORD_TOKEN')
    if not discord_token:
        print("❌ DISCORD_TOKEN이 설정되지 않았습니다.")
        exit(1)
    
    anthropic_key = os.getenv('ANTHROPIC_API_KEY')
    if not anthropic_key:
        print("❌ ANTHROPIC_API_KEY가 설정되지 않았습니다.")
        exit(1)
    
    print("🚀 Claude AI 디스코드 봇을 시작합니다...")
    bot.run(discord_token)