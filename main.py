import os
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import commands
import anthropic

# 환경변수 로드
load_dotenv()

# Anthropic 클라이언트 초기화
anthropic_client = anthropic.Anthropic(
    api_key=os.getenv('ANTHROPIC_API_KEY')
)

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

# Claude AI에게 질문하기
async def ask_claude(question: str, user_info: str = '') -> str:
    try:
        system_prompt = f"""당신은 디스코드 봇으로 작동하는 도움이 되는 AI 어시스턴트입니다. 
사용자의 질문에 친근하고 유용한 답변을 제공해주세요. 
답변은 1800자를 넘지 않도록 해주세요.
필요하다면 코드 블록이나 마크다운을 사용해서 가독성을 높여주세요.
{user_info}"""

        message = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": question
                }
            ]
        )
        
        return message.content[0].text
        
    except anthropic.AuthenticationError:
        return "❌ API 키가 유효하지 않습니다. 봇 관리자에게 문의하세요."
    except anthropic.RateLimitError:
        return "⏳ 요청이 너무 많습니다. 잠시 후 다시 시도해주세요."
    except anthropic.APIStatusError as e:
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

# AI 요청 처리
async def handle_ai_request(message, question: str):
    async with message.channel.typing():
        try:
            print(f"[{message.author.name}] 질문: {question}")
            
            user_info = f"사용자 정보: {message.author.name} (Discord)"
            response = await ask_claude(question, user_info)
            
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
    if message.author == bot.user:
        return
    
    await bot.process_commands(message)
    
    # DM에서는 모든 메시지에 응답
    if isinstance(message.channel, discord.DMChannel):
        await handle_ai_request(message, message.content)
        return
    
    # 서버에서는 !ai 명령어나 봇 멘션에만 응답
    bot_mention = f'<@{bot.user.id}>'
    is_mentioned = bot_mention in message.content
    is_command = message.content.startswith('!ai ')
    
    if not is_mentioned and not is_command:
        return
    
    # 질문 추출
    if is_mentioned:
        question = message.content.replace(bot_mention, '').strip()
    elif message.content.startswith('!ai '):
        question = message.content[4:].strip()
    else:
        return
    
    if not question:
        embed = discord.Embed(
            title="🤖 Claude AI 봇 사용법",
            color=0x0099ff
        )
        embed.add_field(
            name="💬 질문하기",
            value="`!ai [질문]` 또는 `@봇멘션 [질문]`",
            inline=False
        )
        embed.add_field(
            name="📝 예시",
            value="`!ai 파이썬으로 피보나치 수열 만드는 방법`",
            inline=False
        )
        
        await message.reply(embed=embed)
        return
    
    await handle_ai_request(message, question)

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
        value="• `!ai [질문]` - AI에게 질문하기\n• `@봇멘션 [질문]` - 멘션으로 질문하기\n• **DM으로 메시지** - 바로 AI 응답",
        inline=False
    )
    
    embed.add_field(
        name="📝 사용 예시",
        value="• `!ai 파이썬으로 웹크롤러 만드는 방법`\n• `!ai 언리얼 엔진 C++ vs 블루프린트 차이점`\n• `!ai 오늘 저녁 메뉴 추천해줘`",
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
        value="• `!ai [question]` - Ask AI\n• `@bot_mention [question]` - Mention bot\n• **DM message** - Direct AI response",
        inline=False
    )
    
    embed.add_field(
        name="📝 Examples",
        value="• `!ai How to make a web crawler in Python`\n• `!ai Unreal Engine C++ vs Blueprint differences`\n• `!ai Recommend dinner menu`",
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
    embed.add_field(name="🤖 AI 모델", value="Claude Sonnet 4", inline=True)
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