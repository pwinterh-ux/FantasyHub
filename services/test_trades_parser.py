from services.mfl_trades_parsers import parse_pending_trades, normalize_trades_for_template
from pprint import pprint

xml = b'''<pendingTrades>
<pendingTrade trade_id="1204" will_receive="16584,FP_0006_2026_1,FP_0005_2027_2," comments="testing apis. please leave open for a bit" will_give_up="12263,FP_0001_2026_2," description="..." offeredto="0001" offeringteam="0008" timestamp="1757094050" expires="1757696400"/>
</pendingTrades>'''

rows = parse_pending_trades(xml)
print("Parsed trades:", rows)

table = normalize_trades_for_template(
    rows, my_fid="0001", league_id="55188", league_name="Dynasty Awesome Sauce",
    base_url="https://www43.myfantasyleague.com", year=2025,
    team_name_by_fid={"0001":"You","0008":"Other GM"}
)
pprint(table)
