"""Common English words, for the typo step: a seeded typo never turns one word into another
real one (fix wave G1, from rehearsal 2: "still" became "sill"). Short and everyday words
matter most, since a typo of a chat word lands on a chat word: "good" -> "god", "your" ->
"you", "form" <-> "from", "loose" <- "lose"."""

COMMON_WORDS = frozenset("""
a able about above across act add after again age ago agree ah ahead aid aim air all allow
almost alone along already also always am among an and anger angle angry animal another answer
ant any anyone anything apart ape apple are area arm army around art as ask at ate aunt away
awe axe baby back bad bag bake ball ban band bank bar bare barn base bat bath bay be bead beam
bean bear beard beat bed bee been beer before began begin being bell belt ben bench bend best
bet better between big bike bill bin bird bit bite black blade blame blank blast bleed blind
block blood blow blue board boat body boil bold bolt bomb bond bone book boom boot bore born
boss both bow bowl box boy brain brave bread break breed brick bride bring broad broke brown
bud bug build built bull bump bun burn burst bus bush busy but buy by cab cake call calm came
camp can cane cap car card care cart case cash cast cat catch cause cave cell cent chain chair
chance change chat cheap check cheer chef chest chief child chin chip choice choose chop chose
city clap class clean clear climb clip clock close cloth cloud club coach coal coat cod code
coin cold come cone cook cool cop cope copy core corn cost cot cough could count court cover
cow crab crash crew crop cross crow crowd cry cub cup cure curl cut dad dam damp dance dare dark
date dawn day dead deaf deal dear debt deck deep deer den deny desk dial did die diet dig dim
dine diner dinner dip dirt dish do dock does dog doll done door dot dove down drag draw dream
dress drew drink drip drive drop drum dry duck due dug dull dumb dust duty each ear earn ease
east easy eat edge egg eight either else end enemy enjoy enough enter equal even ever every evil
exit eye face fact fade fail fair fall fame fan far farm fast fat fate fear feast fed fee feed
feel fees feet fell felt fence few field fig fight file fill film find fine fire firm first
fish fit five fix flag flame flat flaw fled flew flip float flock flood floor flow fly foam fog
fold folk follow food fool foot for force fork form fort found four fox free fresh friend frog
from front frost fruit fry full fun fund fur gain game gap gas gate gave gear gem get ghost
gift girl give glad glass glow glue go goal goat god gold golf gone good got grab grade grain
grand grass grave gray great green grew grey grid grin grip ground group grow guard guess guest
guide gun guy had hair half hall halt ham hand hang hard harm hat hate have he head heal hear
heard heart heat heel held hell hello help hen her herb here hero hers hide high hill him hint
hip hire his hit hold hole holy home hood hook hop hope hopping horn horse host hot hour house
how hug huge hull hum hung hunt hurt hut ice idea if ill in inch into iron is it its jam jar jaw
jet job jog join joke joy judge jug jump just keen keep kept key kick kid kill kind king kiss
kit knee knew knife knock know lab lack lad lady laid lake lamb lamp land lane lap large last
late later latter laugh law lawn lay lazy lead leaf lean leap learn least leave led left leg
lend less let lid lie life lift light like lime line link lion lip list lit live load loan lock
log lone long look loop loose lord lose loss lost lot loud love low luck lung mad made mail main
make male man many map mark mash mass mat match mate may me meal mean meat meet melt men mend
mess met mice mid might mile milk mind mine miss mist mix mob mode mom moon more most mother
move much mud mug must my nail name nap near neat neck need net never new news next nice night
nine no nod none noon nor north nose not note now nut oak oat odd of off often oil old on once
one only open or order other our out oven over owe owl own pace pack pad page paid pain pair
pal palm pan paper park part pass past pat path paw pay pea peace peak pear pen pet pick pie
piece pig pile pill pin pine pink pint pipe pit place plan plane plant plate play plot plug
plus pod poem point pole pool poor pop port pose post pot pour power pray press pretty price
pride print pull pump punch pup pure push put quick quiet quit quite race rag rage raid rail
rain raise ran rank rare rat rate raw ray reach read ready real rear red rent rest rice
rid ride right ring rip rise risk road roar rob rock rod role roll roof room root rope rose rot
rough round row rub rug rule run rush rust sack sad safe said sail salt same sand sang sat save
saw say scare scared sea seal seat see seed seek seem seen self sell send sent set seven shade
shake shall shame shape share she shed shell shift shin ship shirt shoe shoot shop short shot
should show shut shy sick side sigh sight sign silk sill sin sing sink sir sit six size ski skin
skip sky slap sleep slid slide slim slip slow small smell smile snap snow so soap sock soft soil
sold some son song soon sore sort soul sound soup south space spare speak speed spell spend
spin spit spoke spot spy stack staff stage stair stake stand star start state stay steal steam
step stick still sting stir stock stole stone stood stool stop store storm story stove strap
straw stray strip study stuff such suit sum sun super supper sure swap swim tab tag tail take
tale talk tall tan tank tap tape task taste tax tea team tear tell ten tend tent term test than
thank that the thee them then there these they thin thing think this those though three threw
throw tide tie till time tin tip tire to toe told tone too took tool top torn toss total touch
tour town toy track trade trail train trap tree trial trick trip true trust truth try tub tune
turn twin two type ugly under unit until untied united up upon us use used van vast vet view
vow wade wage wait wake walk wall want war warm warn was wash waste watch water wave wax way we
weak wear web wed wee week weep well went were west wet what wheat wheel when where which while
whip white who whole why wide wife wild will win wind wine wing wipe wire wise wish wit with
woke wolf woman won wood wool word wore work world worm worn worse worst would wrap write wrong
yard yarn year yes yet you young your yours zone
""".split())
