"""Forty invented long drafts whose answer sits in the second or third clause (review of #370):
(his line, her draft, a marker of the answer, kind: fact, num or yn). No persona or chat text."""
DRAFTS = [
("what drops from patchwerk?", "oh man that fight, i always forget half the table, but he drops the plate shoulders and the caster dagger", "plate shoulders", "fact"),
("where is the entrance to ulduar?", "lol you always ask me this, honestly i get lost too, it's in storm peaks north of the temple", "storm peaks", "fact"),
("who tanks the adds on gluth?", "hmm good one, we changed it last week after the wipe, the bear kites them in the back", "bear", "fact"),
("what gem goes in a red socket for me?", "depends on who you ask really, everyone has opinions about gems, but go with the bold cardinal ruby", "cardinal ruby", "fact"),
("when does the raid start tonight?", "ugh my evening is a mess already, dinner is late and all, but we start at eight server time", "eight", "fact"),
("why did we wipe on that pull?", "honestly i was barely looking at the screen, my cat jumped up, but the healers were out of range", "out of range", "fact"),
("how do i get to the wintergrasp vendor?", "you would think they made it obvious, i wandered around for ages, take the portal in dalaran then go left", "portal in dalaran", "fact"),
("which trinket is better for a mage?", "that's a fun argument we had in guild chat, people got loud, but the dying curse wins by a mile", "dying curse", "fact"),
("what's the trick on heigan?", "everyone panics on that one the first time, i did too, you just follow the tank through the safe zones", "safe zones", "fact"),
("who has the key for the vault?", "i asked the same thing yesterday and nobody knew, then it turned up, the guild bank officer has it", "bank officer", "fact"),
("where do you farm saronite?", "i used to hate mining so much, such a chore, but sholazar basin along the river is the spot", "sholazar", "fact"),
("what spec are you running tonight?", "been going back and forth all week on it, the sims are close, i'm going frost for the cleave", "frost", "fact"),
("how many healers do we need for 25?", "people argue about this every single raid night, it never ends, i would bring six for a first clear", "six", "num"),
("what's the enrage timer on patchwerk?", "people always forget this one, i did too on my first night, it's six minutes flat", "six minutes", "num"),
("how much dkp do i have?", "let me look at the sheet, the officer page is slow tonight, you're sitting at forty two", "forty two", "num"),
("how many badges for the chest piece?", "i bought mine ages ago so bear with me, it might have changed, i think it was 80 emblems", "80", "num"),
("when is the weekly reset?", "it always sneaks up on me, i lose track of the days, it's tuesday morning at nine", "tuesday", "num"),
("how long is the run back from the graveyard?", "way too long if you ask me, they should fix it, but about two minutes on foot", "two minutes", "num"),
("what level do i need for the heroic?", "they changed the rules a few times over the years, hard to keep up, you need to be 80 now", "80", "num"),
("how many stacks before the tanks swap?", "depends on the gear a bit and the healers, we were sloppy last time, swap at three stacks", "three stacks", "num"),
("are you coming to the raid tonight?", "so about tonight, i checked with the family and work, yes i'm in because the shift got moved", "yes", "yn"),
("did you get the shield from the last boss?", "funny you ask, there was a whole fight over that roll, no it went to the other tank since he had more points", "no it went", "yn"),
("is frost better than unholy for this fight?", "i went over the logs this morning with coffee, took a while, yes frost wins because of the cleave", "yes frost", "yn"),
("can you craft the belt buckle for me?", "let me check my recipes real quick, the list is long, yeah i can since i trained it last week", "yeah i can", "yn"),
("do we need a second tank for this?", "we talked about it in officer chat for a bit, opinions were split, no one tank is fine because the adds die fast", "no one tank", "yn"),
("should i use my cooldowns on the pull?", "everyone does this differently and it shows in the logs, but yes pop them early since the fight is short", "yes pop", "yn"),
("is the boss tauntable?", "i had to look that up myself the other day, embarrassing, no he is immune so watch your threat", "no he is immune", "yn"),
("are we doing hard modes this week?", "the officers went back and forth on it all weekend, long thread, yes but only the first two since people are tired", "yes but", "yn"),
("did the guild bank get the flasks?", "i was just looking at the tabs a minute ago, it's a mess in there, yes forty of them came in today", "yes forty", "yn"),
("can i bring my alt tonight?", "thats a question for the raid lead normally, but i asked already, yes as long as the gear is fine", "yes as long", "yn"),
("what should i enchant my weapon with?", "Good question. I went through this last month myself. Berserking is the one you want for melee.", "Berserking", "fact"),
("where's the summoning stone?", "Oh I know this one. Took me forever to find it the first time. It's right outside the gate on the left.", "outside the gate", "fact"),
("who is leading tonight?", "Not me thank god. I did it twice last week and I'm fried. The bear tank has it tonight.", "bear tank", "fact"),
("how many people signed up?", "I checked the calendar a minute ago. It was looking thin earlier. We have 22 now.", "22", "num"),
("is the door boss skippable?", "People keep asking that. I tested it on my alt. No, you have to kill him because the gate stays shut.", "No, you have", "yn"),
("why is my dps so low?", "Don't beat yourself up. Everybody has bad nights. Your hit rating is under the cap so you miss a lot.", "hit rating", "fact"),
("when did we first kill him?", "That was a great night. I still have the screenshot somewhere. It was back in march i think.", "march", "num"),
("what's the best food buff for a healer?", "I cook way too much in this game. My bags are full of it. Bring the spell power fish feast.", "fish feast", "fact"),
("do you have a spare flask?", "Let me dig through my bags. So much junk in here. Yes, i have two so take one.", "Yes, i have", "yn"),
("which boss drops the mount?", "Ha, everyone wants that thing. I've run it forty times. It's the last boss in the dragon wing.", "last boss", "fact"),
]

# Thirty-two more (re-review of #370): a lead-in that holds a false proof (a number, a yes or
# no word, a capital, "forgot"), the answer in the last clause, lower-case names, two questions
# from him, quoted lines, an emoji tail. A marker may be a tuple: every part must be kept.
ADVERSARIAL = [
# answer in the last clause
("where is the entrance to ulduar?", "lol you ask me this every Tuesday. it's in storm peaks north of the temple", "storm peaks", "last"),
("who tanks the adds on gluth?", "not sure why you ask me of all people lol. the bear kites them in the back", "bear", "last"),
("what gem goes in a red socket?", "i forgot my coffee again this morning so bear with me. bold cardinal ruby is the one", "cardinal ruby", "last"),
("why did we wipe on that pull?", "no idea how the tank survived that long honestly. the healers were out of range", "out of range", "last"),
("which trinket is better for a mage?", "funny, Mirela asked me the same thing yesterday. the dying curse wins by a mile", "dying curse", "last"),
("how do i get to the vendor?", "everyone in Dalaran gets lost looking for it. take the sewers entrance then go left", "sewers", "last"),
# numeric: a number in the lead-in
("when does the raid start tonight?", "ive told you two times already lol. we start at eight server time", "eight", "last"),
("how many flasks do i need?", "third raid in a row you ask, that's 3 now. bring six for the whole night", "six", "last"),
("what time is the meeting?", "half the guild keeps asking me that today. it's at nine after the raid", "nine", "last"),
("how long is the fight?", "we wiped 4 times on it last week. about seven minutes if dps is fine", "seven", "last"),
# lower-case name
("who pulled the boss early?", "it was a mess from the start honestly, everyone was eating, gorak pulled it with his pet", "gorak", "lcname"),
("who is leading tonight?", "we argued about it all afternoon in officer chat, then voted, belra leads tonight", "belra", "lcname"),
("who won the roll on the helm?", "there were five of us rolling and it took ages. coros won it with a 98", "coros", "lcname"),
("who should i ask about enchants?", "Honestly half the guild has no clue. ask mirela she has every recipe", "mirela", "lcname"),
("which healer is on the tank?", "we swapped it around after last Thursday. dorn is on the tank now", "dorn", "lcname"),
# yes/no buried after a lead-in
("are you coming tonight?", "oh no not this again lol. yeah i'll be there right after dinner", "yeah", "yn"),
("is he tauntable?", "no way you forgot that already haha. yes he is tauntable after the first phase", "yes he is", "yn"),
("can you tank it?", "maybe ask me when i'm awake next time lol. no i can't, my gear is all dps", "no i can't", "yn"),
("did we kill him last week?", "sure feels like forever ago doesn't it. no we wiped at five percent twice", "no we wiped", "yn"),
("do you have the recipe?", "probably the tenth time someone asks today. yes i have it, send me the mats", "yes i have", "yn"),
("you around later?", "definitely a weird day at work so far. no, i'm out tonight, sorry", "no,", "yn"),
# two questions from him
("what time does it start? and are you coming?", "it starts at eight server time. and yes i'll be there once the kids are in bed", ("eight", "yes"), "twoq"),
("are you coming? and what time does it start?", "yeah i'm coming for sure. it starts at eight i think, bring flasks", ("yeah", "eight"), "twoq"),
("who leads tonight and how many healers do we take?", "Belra leads tonight like always. we take six healers for the heroic bosses", ("belra", "six"), "twoq"),
("did you get the mats? how many do you need still?", "yes got most of them from the bank. i still need twelve of the ore though", ("yes", "twelve"), "twoq"),
# quoted
("what did he say about the loot?", 'he was annoyed about the whole thing honestly. he said "the helm goes to the tank, end of story" and logged', "helm goes to the tank", "quote"),
("when did she say we start?", 'she posted like 3 messages about food first. then "we start at eight, be early" in guild chat', "eight", "quote"),
("did he say yes?", 'no clue what he meant at first. then he said "yes, fine, i will tank it" and left', "yes, fine", "quote"),
# emoji / emoticon tail
("where do we meet?", "lol every time with you :) outside the instance at the summoning stone :D :D", "summoning stone", "emoji"),
("are you coming tonight?", "ugh long day, kid is sick, work was bad. but yes 😅😅", "yes", "emoji"),
("how many do we need?", "depends who shows up really, last week was a mess. ten 🙃", "ten", "emoji"),
("who has the key?", "we passed it around all week and nobody remembers lol. dorn has it 😂", "dorn", "emoji"),
]

# Twenty more (re-review round 2 of #370): the answer split across two sentences, a number, a
# clock time or a quoted yes or no in the lead-in, and answers that hold no token at all.
FRESH = [
# answer split across two clauses / sentences
("are you coming tonight?", "yes for the first two bosses at least. after ten i have to leave though, early shift tomorrow", ("yes", "after ten"), "split"),
("can you tank it?", "probably the first half of the instance. no chance on the last boss though, my gear is too weak", ("probably", "no chance"), "split"),
("when does the raid start?", "invites go out at seven like always. first pull is at eight though, so be early", ("seven", "eight"), "split"),
("how many healers do we take?", "five for the normal bosses is plenty. take six for the last one though, it hits hard", ("five", "six"), "split"),
("is he tauntable?", "yes in the first phase he is. in the second one he isn't, so swap before it", ("yes", "isn't"), "split"),
# a number in the lead-in that is not the answer, answer has no number token
("when does the raid start?", "we wiped 4 times last week because people were late. it starts right after the officers meeting", "after the officers meeting", "leadnum"),
("how many flasks do i need?", "you asked me this 3 days ago lol. a couple for each boss should do it", "a couple", "leadnum"),
("how long is the fight?", "took us 2 nights to learn it honestly. it goes on until the adds stop spawning, pretty short", "until the adds", "leadnum"),
("what level do i need for it?", "my alt got in at 78 by accident once haha. you need the cap, no way around it", "the cap", "leadnum"),
("when are you back?", "ive been up since 5 with the baby. back once she is asleep again, cant say exactly", "once she is asleep", "leadnum"),
# "yes" quoted from him, real answer without a yes/no word
("so you are coming then?", 'you wrote "yes" for me on the sheet without asking lol. i cant make it, work swapped my shift', "cant make it", "quoted"),
("did he agree?", 'he typed "sure" and then went quiet for an hour. in the end he refused, flat out', "refused", "quoted"),
("is it fixed now?", 'the patch notes say "yes, resolved" in big letters. it still breaks for me every single pull', "still breaks", "quoted"),
("are we doing heroic?", 'half the raid shouted "no" in voice when i asked. we are doing it anyway, i decided', "doing it anyway", "quoted"),
("you ok with that plan?", 'i said "maybe" yesterday because i was tired. i am fully on board with it now', "fully on board", "quoted"),
# a time in a lead-in
("when does it start?", "its 2am here so my brain is mush. it starts once the first group is done", "once the first group", "leadtime"),
("what time is the meeting?", "i only got home at 6 so i havent checked. straight after the raid, same as usual", "after the raid", "leadtime"),
("when do you finish work?", "started at 7 today, long one. i finish whenever the last delivery shows up", "last delivery", "leadtime"),
("when can you log on?", "dinner was at 5 and im still stuffed. i can log on as soon as the kids settle", "kids settle", "leadtime"),
("how long till you are here?", "left the house at noon like an idiot. not long, im parking the car now", "parking the car", "leadtime"),
]
