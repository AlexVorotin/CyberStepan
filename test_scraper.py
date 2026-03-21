import asyncio, logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

async def main():
    from scraper import get_schedule
    from bot import _format_message
    print("Fetching...")
    result = await get_schedule("С. Кощіївка", "Вул. Лісова", "1а")
    if not result: print("No result"); return
    print(f"Queue: {result.get('_queue')}")
    print(f"Today non-on: {[f'{s}:{v}' for s,v in result.get('_today',{}).items() if v!='on']}")
    print(f"Tomorrow non-on: {[f'{s}:{v}' for s,v in result.get('_tomorrow',{}).items() if v!='on']}")
    print("\n" + "="*40)
    print(_format_message(result))

asyncio.run(main())
