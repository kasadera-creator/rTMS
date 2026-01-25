from __future__ import annotations
from typing import Dict, List, Tuple, Any

# Instrument ordering for patient workflow
INSTRUMENT_ORDER: List[str] = [
    "bdi2",
    "sds",
    "sassj",
    "phq9",
    "stai_x1",
    "stai_x2",
    "dai10",
]
INSTRUMENT_SET = set(INSTRUMENT_ORDER)


def _opts(*pairs: Tuple[str, str, int]):
    """Helper to build option dicts from tuples of (id, label, score)."""
    return [
        {"id": pid, "label": label, "score": score}
        for pid, label, score in pairs
    ]


BDI2_QUESTIONS: List[Dict[str, Any]] = [
    {
        "key": "q1",
        "text": "悲しさ",
        "options": _opts(
            ("0", "わたしは気が滅入っていない", 0),
            ("1", "しばしば気が滅入る", 1),
            ("2", "いつも気が滅入っている", 2),
            ("3", "とても気が滅入ってつらくて耐えがたい", 3),
        ),
    },
    {
        "key": "q2",
        "text": "悲観",
        "options": _opts(
            ("0", "将来について悲観していない", 0),
            ("1", "以前よりも将来について悲観的に感じる", 1),
            ("2", "物事が自分にとってうまくいくとは思えない", 2),
            ("3", "将来は絶望的で悪くなるばかりだと思う", 3),
        ),
    },
    {
        "key": "q3",
        "text": "過去の失敗",
        "options": _opts(
            ("0", "自分を落伍者だとは思わない", 0),
            ("1", "普通の人より失敗が多かったと思う", 1),
            ("2", "人生を振り返ると失敗ばかりを思い出す", 2),
            ("3", "自分は人間として完全な落伍者だと思う", 3),
        ),
    },
    {
        "key": "q4",
        "text": "喜びの喪失",
        "options": _opts(
            ("0", "自分が楽しいことには以前と同じくらい喜びを感じる", 0),
            ("1", "以前ほど物事を楽しめない", 1),
            ("2", "以前は楽しめたことにもほとんど喜びを感じなくなった", 2),
            ("3", "以前は楽しめたことにもまったく喜びを感じなくなった", 3),
        ),
    },
    {
        "key": "q5",
        "text": "罪責感",
        "options": _opts(
            ("0", "特に罪の意識はない", 0),
            ("1", "自分のしたことやすべきだったことの多くに罪悪感を感じる", 1),
            ("2", "ほとんどいつも罪悪感を感じている", 2),
            ("3", "絶えず罪悪感を感じている", 3),
        ),
    },
    {
        "key": "q6",
        "text": "被罰感",
        "options": _opts(
            ("0", "自分が罰を受けているようには感じない", 0),
            ("1", "自分は罰を受けるかもしれないと思う", 1),
            ("2", "自分は罰を受けるだろう", 2),
            ("3", "自分は今罰されていると感じる", 3),
        ),
    },
    {
        "key": "q7",
        "text": "自己嫌悪",
        "options": _opts(
            ("0", "自分自身に対する意識は以前と変わらない", 0),
            ("1", "自分自身に対して自信をなくした", 1),
            ("2", "自分自身に失望している", 2),
            ("3", "自分自身が嫌でたまらない", 3),
        ),
    },
    {
        "key": "q8",
        "text": "自己批判",
        "options": _opts(
            ("0", "以前よりも自分自身に批判的ということはない", 0),
            ("1", "以前より自分自身に批判的だ", 1),
            ("2", "あらゆる自分の欠点が気になり自分を責めている", 2),
            ("3", "何か悪いことが起こると、全て自分のせいだと思う", 3),
        ),
    },
    {
        "key": "q9",
        "text": "自殺念慮",
        "options": _opts(
            ("0", "自殺したいと思うことはまったくない", 0),
            ("1", "自殺したいと思うことはあるが、本当にしようとは思わない", 1),
            ("2", "自殺したいと思う", 2),
            ("3", "機会があれば自殺するだろう", 3),
        ),
    },
    {
        "key": "q10",
        "text": "落涙",
        "options": _opts(
            ("0", "以前よりも涙もろいということはない", 0),
            ("1", "以前より涙もろい", 1),
            ("2", "どんなささいなことにも涙が出る", 2),
            ("3", "泣きたいと感じるのに涙が出ない", 3),
        ),
    },
    {
        "key": "q11",
        "text": "激越",
        "options": _opts(
            ("0", "普段以上に落ち着きがなかったり緊張しやすくはない", 0),
            ("1", "普段より落ち着きがなかったり緊張しやすい", 1),
            ("2", "気持ちが落ち着かずじっとしているのが難しい", 2),
            ("3", "気持ちが落ち着かず絶えず動いたり何かしていないと気が済まない", 3),
        ),
    },
    {
        "key": "q12",
        "text": "興味喪失",
        "options": _opts(
            ("0", "他の人や活動に対する関心を失ってはいない", 0),
            ("1", "以前より他の人や物事に対する関心が減った", 1),
            ("2", "他の人や物事への関心がほとんどなくなった", 2),
            ("3", "何事にも興味をもつことが難しい", 3),
        ),
    },
    {
        "key": "q13",
        "text": "決断力低下",
        "options": _opts(
            ("0", "以前と同じように物事を決断できる", 0),
            ("1", "以前より決断するのが難しくなった", 1),
            ("2", "以前より決断するのがずっと難しくなった", 2),
            ("3", "どんなことを決めるにもひどく苦労する", 3),
        ),
    },
    {
        "key": "q14",
        "text": "無価値感",
        "options": _opts(
            ("0", "自分に価値がないとは思わない", 0),
            ("1", "以前ほど自分に価値があり人の役に立てる人間だと思えない", 1),
            ("2", "他の人に比べて自分は価値がないと思う", 2),
            ("3", "自分はまったく価値がないと思う", 3),
        ),
    },
    {
        "key": "q15",
        "text": "活力喪失",
        "options": _opts(
            ("0", "以前と同じように活力がある", 0),
            ("1", "以前と比べて活力が減った", 1),
            ("2", "活力が足りなくて十分動けない", 2),
            ("3", "活力がなく何もできない", 3),
        ),
    },
    {
        "key": "q16",
        "text": "睡眠習慣の変化",
        "options": _opts(
            ("0", "睡眠習慣に変わりはない", 0),
            ("1a", "以前より少し睡眠時間が長い", 1),
            ("1b", "以前より少し睡眠時間が短い", 1),
            ("2a", "以前よりかなり睡眠時間が長い", 2),
            ("2b", "以前よりかなり睡眠時間が短い", 2),
            ("3a", "ほとんど一日中寝ている", 3),
            ("3b", "以前より1～2時間早く目がさめて、再び眠れない", 3),
        ),
    },
    {
        "key": "q17",
        "text": "易刺激性",
        "options": _opts(
            ("0", "普段よりイライラしやすいわけではない", 0),
            ("1", "普段よりイライラしやすい", 1),
            ("2", "普段よりかなりイライラしやすい", 2),
            ("3", "いつもイライラしやすい", 3),
        ),
    },
    {
        "key": "q18",
        "text": "食欲の変化",
        "options": _opts(
            ("0", "食欲は以前と変わらない", 0),
            ("1a", "以前より少し食欲が落ちた", 1),
            ("1b", "以前より少し食欲が増えた", 1),
            ("2a", "以前よりかなり食欲が落ちた", 2),
            ("2b", "以前よりかなり食欲が増えた", 2),
            ("3a", "まったく食欲がなくなった", 3),
            ("3b", "いつも何か食べたくてたまらない", 3),
        ),
    },
    {
        "key": "q19",
        "text": "集中困難",
        "options": _opts(
            ("0", "以前と同じように集中できる", 0),
            ("1", "以前ほどは集中できない", 1),
            ("2", "何事にも長い間集中することは難しい", 2),
            ("3", "何事にも集中できない", 3),
        ),
    },
    {
        "key": "q20",
        "text": "疲労感",
        "options": _opts(
            ("0", "以前と比べて疲れやすいわけではない", 0),
            ("1", "以前より疲れやすい", 1),
            ("2", "以前ならできた多くのことが疲れてしまってできない", 2),
            ("3", "以前ならできたほとんどのことが疲れてしまってできない", 3),
        ),
    },
    {
        "key": "q21",
        "text": "性欲減退",
        "options": _opts(
            ("0", "性欲は以前と変わらない", 0),
            ("1", "以前ほど性欲がない", 1),
            ("2", "最近めっきり性欲が減退した", 2),
            ("3", "まったく性欲がなくなった", 3),
        ),
    },
]

SDS_OPTIONS = _opts(
    ("1", "ないかたまに", 1),
    ("2", "ときどき", 2),
    ("3", "かなりのあいだ", 3),
    ("4", "ほとんどいつも", 4),
)

SDS_QUESTIONS: List[Dict[str, Any]] = [
    {"key": "q1", "text": "気が沈んで憂うつだ", "options": SDS_OPTIONS},
    {"key": "q2", "text": "朝方はいちばん気分がよい", "options": SDS_OPTIONS},
    {"key": "q3", "text": "泣いたり、泣きたくなる", "options": SDS_OPTIONS},
    {"key": "q4", "text": "夜よく眠れない", "options": SDS_OPTIONS},
    {"key": "q5", "text": "食欲はふつうだ", "options": SDS_OPTIONS},
    {"key": "q6", "text": "異性に対する関心がある (まだ性欲がある)", "options": SDS_OPTIONS},
    {"key": "q7", "text": "やせてきたことに気がつく", "options": SDS_OPTIONS},
    {"key": "q8", "text": "便秘している", "options": SDS_OPTIONS},
    {"key": "q9", "text": "ふだんよりも動悸がする", "options": SDS_OPTIONS},
    {"key": "q10", "text": "何となく疲れる", "options": SDS_OPTIONS},
    {"key": "q11", "text": "気持ちはいつもさっぱりしている", "options": SDS_OPTIONS},
    {"key": "q12", "text": "いつもと変わりなく仕事をやれる", "options": SDS_OPTIONS},
    {"key": "q13", "text": "落ち着かず、じっとしていられない", "options": SDS_OPTIONS},
    {"key": "q14", "text": "将来に希望がある", "options": SDS_OPTIONS},
    {"key": "q15", "text": "いつもよりイライラする", "options": SDS_OPTIONS},
    {"key": "q16", "text": "たやすく決断できる", "options": SDS_OPTIONS},
    {"key": "q17", "text": "役に立つ、働ける人間だと思う", "options": SDS_OPTIONS},
    {"key": "q18", "text": "生活はかなり充実している", "options": SDS_OPTIONS},
    {"key": "q19", "text": "自分が死んだほうが他の者は楽に暮らせると思う", "options": SDS_OPTIONS},
    {"key": "q20", "text": "日頃していることに満足している", "options": SDS_OPTIONS},
]

SASSJ_OPTIONS_INTEREST = _opts(
    ("0", "大変興味がある", 0),
    ("1", "まあまあ興味がある", 1),
    ("2", "少し興味がある", 2),
    ("3", "全く興味がない", 3),
)
SASSJ_OPTIONS_ENJOY = _opts(
    ("0", "大変楽しい", 0),
    ("1", "まあまあ楽しい", 1),
    ("2", "少し楽しい", 2),
    ("3", "全く楽しくない", 3),
)
SASSJ_OPTIONS_FREQ = _opts(
    ("0", "大変頻繁に", 0),
    ("1", "まあまあ頻繁に", 1),
    ("2", "まれにしか", 2),
    ("3", "全く", 3),
)
SASSJ_OPTIONS_GOOD = _opts(
    ("0", "大変良い", 0),
    ("1", "良い", 1),
    ("2", "まあまあ良い", 2),
    ("3", "悪い", 3),
)
SASSJ_OPTIONS_COUNT = _opts(
    ("0", "大勢いる", 0),
    ("1", "何人かいる", 1),
    ("2", "少しいる", 2),
    ("3", "一人もいない", 3),
)
SASSJ_OPTIONS_BUILD = _opts(
    ("0", "大変積極的", 0),
    ("1", "積極的", 1),
    ("2", "それなり", 2),
    ("3", "ほとんどしない", 3),
)
SASSJ_OPTIONS_VALUE = _opts(
    ("0", "大変重視", 0),
    ("1", "重視", 1),
    ("2", "少し重視", 2),
    ("3", "全く重視していない", 3),
)
SASSJ_OPTIONS_RULE = _opts(
    ("0", "いつも守る", 0),
    ("1", "だいたい守る", 1),
    ("2", "あまり守らない", 2),
    ("3", "全く守らない", 3),
)
SASSJ_OPTIONS_PARTICIPATE = _opts(
    ("0", "全面的に参加", 0),
    ("1", "まあまあ参加", 1),
    ("2", "少ししか参加", 2),
    ("3", "全く参加していない", 3),
)
SASSJ_OPTIONS_LIKE = _opts(
    ("0", "大変好き", 0),
    ("1", "まあまあ好き", 1),
    ("2", "それほど好きではない", 2),
    ("3", "嫌い", 3),
)
SASSJ_OPTIONS_DIFFICULTY = _opts(
    ("0", "全く困難を感じない", 0),
    ("1", "時々感じる", 1),
    ("2", "しばしば感じる", 2),
    ("3", "いつも感じる", 3),
)
SASSJ_QUESTIONS: List[Dict[str, Any]] = [
    {
        "key": "q1",
        "text": "何か仕事をしていますか (在職中ですか)",
        "options": _opts(("yes", "はい", 0), ("no", "いいえ", 3)),
    },
    {
        "key": "q2",
        "text": "今の仕事に興味がありますか / 家事に興味がありますか",
        "options": SASSJ_OPTIONS_INTEREST,
        "dynamic_label": {
            "source_key": "q1",
            "cases": {
                "yes": "今の仕事に興味がありますか",
                "no": "家事に興味がありますか",
            },
        },
    },
    {"key": "q3", "text": "今の仕事や家事を楽しんでやっていますか", "options": SASSJ_OPTIONS_ENJOY},
    {"key": "q4", "text": "趣味・余暇に興味がありますか", "options": SASSJ_OPTIONS_INTEREST},
    {"key": "q5", "text": "余暇は充実していますか", "options": SASSJ_OPTIONS_ENJOY},
    {"key": "q6", "text": "家庭とどのくらい頻繁にコミュニケーションをとりますか", "options": SASSJ_OPTIONS_FREQ},
    {"key": "q7", "text": "家族関係は良いですか", "options": SASSJ_OPTIONS_GOOD},
    {"key": "q8", "text": "家族以外で親しくしている人はどれぐらいいますか", "options": SASSJ_OPTIONS_COUNT},
    {"key": "q9", "text": "他人との関係を積極的に築こうとしますか", "options": SASSJ_OPTIONS_BUILD},
    {"key": "q10", "text": "全体として、あなたと他人との関係は良いですか", "options": SASSJ_OPTIONS_GOOD},
    {"key": "q11", "text": "他人との関係にどのぐらい価値をおいていますか", "options": SASSJ_OPTIONS_VALUE},
    {"key": "q12", "text": "周りの人たちはどのくらい頻繁にあなたとのコミュニケーションを求めますか", "options": SASSJ_OPTIONS_FREQ},
    {"key": "q13", "text": "社会のルールや礼儀や礼節を守りますか", "options": SASSJ_OPTIONS_RULE},
    {"key": "q14", "text": "地域社会の生活にどのくらい参加していますか", "options": SASSJ_OPTIONS_PARTICIPATE},
    {"key": "q15", "text": "物事や状況や人を理解するため情報を集めるのが好きですか", "options": SASSJ_OPTIONS_LIKE},
    {"key": "q16", "text": "科学や技術や文化に関する情報に興味がありますか", "options": SASSJ_OPTIONS_INTEREST},
    {"key": "q17", "text": "自分の意見を述べるときにどのくらい困難さを感じますか", "options": SASSJ_OPTIONS_DIFFICULTY},
    {"key": "q18", "text": "周囲から受け入れられていない・疎外されていると感じますか", "options": SASSJ_OPTIONS_DIFFICULTY},
    {"key": "q19", "text": "自分の身体的外観をどのくらい気にしていますか", "options": SASSJ_OPTIONS_LIKE},
    {"key": "q20", "text": "財産や収入の管理にどのくらい困難を感じますか", "options": SASSJ_OPTIONS_DIFFICULTY},
    {"key": "q21", "text": "周りの環境を思うままに調整できると感じますか", "options": SASSJ_OPTIONS_GOOD},
]

PHQ9_OPTIONS = _opts(
    ("0", "全くない", 0),
    ("1", "数日", 1),
    ("2", "半分以上", 2),
    ("3", "ほとんど毎日", 3),
)
PHQ9_QUESTIONS: List[Dict[str, Any]] = [
    {"key": "q1", "text": "物事に対してほとんど興味がない、または楽しめない", "options": PHQ9_OPTIONS},
    {"key": "q2", "text": "気分が落ち込む、憂うつになる、または絶望的な気持ちになる", "options": PHQ9_OPTIONS},
    {"key": "q3", "text": "寝付きが悪い、途中で目がさめる、または逆に眠り過ぎる", "options": PHQ9_OPTIONS},
    {"key": "q4", "text": "疲れた感じがする、または気力がない", "options": PHQ9_OPTIONS},
    {"key": "q5", "text": "あまり食欲がない、または食べ過ぎる", "options": PHQ9_OPTIONS},
    {"key": "q6", "text": "自分はダメな人間だ、人生の敗北者だと気に病む、または自分自身あるいは家族に申し訳がないと感じる", "options": PHQ9_OPTIONS},
    {"key": "q7", "text": "新聞を読む、またはテレビを見ることなどに集中することが難しい", "options": PHQ9_OPTIONS},
    {"key": "q8", "text": "他人が気づくぐらいに動きや話し方が遅くなる、あるいは反対に、そわそわしたり動き回ることがある", "options": PHQ9_OPTIONS},
    {"key": "q9", "text": "死んだ方がましだ、あるいは自分を何らかの方法で傷つけようと思ったことがある", "options": PHQ9_OPTIONS},
    {"key": "q10", "text": "問題によって仕事や家事、人づき合いがどのくらい困難か", "options": _opts(("0", "全く困難でない", 0), ("1", "やや困難", 1), ("2", "困難", 2), ("3", "極端に困難", 3)), "include_in_total": False},
]

STAI_X1_OPTIONS = _opts(
    ("1", "全くちがう", 1),
    ("2", "いくらか", 2),
    ("3", "まあそうだ", 3),
    ("4", "その通りだ", 4),
)
STAI_X2_OPTIONS = _opts(
    ("1", "ほとんどない", 1),
    ("2", "ときたま", 2),
    ("3", "しばしば", 3),
    ("4", "しょっちゅう", 4),
)

STAI_X1_QUESTIONS = [
    "気が落ちついている",
    "安心している",
    "緊張している",
    "くよくよしている",
    "気楽だ",
    "気が転倒している",
    "何か悪いことが起こりはしないかと心配だ",
    "心が休まっている",
    "何か気がかりだ",
    "気持ちがよい",
    "自信がある",
    "神経質になっている",
    "気が落ちつかず、じっとしていられない",
    "気がピンと張りつめている",
    "くつろいだ気持ちだ",
    "満ち足りた気分だ",
    "心配がある",
    "非常に興奮して、体が震えるような感じがする",
    "何かうれしい気分だ",
    "気分がよい",
]

STAI_X2_QUESTIONS = [
    "気分がよい",
    "疲れやすい",
    "泣きたい気持ちになる",
    "他の人のように幸せだったらと思う",
    "すぐに心が決まらずチャンスを失いやすい",
    "心が休まっている",
    "落ちついて、冷静で、あわてない",
    "問題が後から後から出てきて、どうしようもないと感じる",
    "つまらないことを心配しすぎる",
    "幸せな気持ちになる",
    "物事を難しく考えてしまう",
    "自信がないと感じる",
    "安心している",
    "危険や困難を避けて通ろうとする",
    "憂うつになる",
    "満ち足りた気分になる",
    "つまらないことで頭が一杯になり、悩まされる",
    "何かで失敗するとひどくがっかりして、そのことが頭を離れない",
    "あせらず、物事を着実に運ぶ",
    "その時気になっていることを考え出すと、緊張したり、動揺したりする",
]

DAI10_OPTIONS = [
    {"id": "agree", "label": "そう思う", "score": 1},
    {"id": "disagree", "label": "そう思わない", "score": -1},
]
DAI10_REVERSE_ITEMS = {2, 5, 6, 8}
DAI10_QUESTIONS = [
    "私の薬は、良いところが多くて、悪いところがすくない。",
    "薬を続けていると、動きがにぶくなって調子が悪い。",
    "薬を飲むことは、私が自分で決めたことだ",
    "薬を飲むと、気持ちがほぐれる",
    "薬を飲むと、疲れてやる気がなくなる",
    "私は、具合が悪いときだけ薬を飲む",
    "薬を続けていると、本来の自分でいられる。",
    "薬が私のこころや体を支配するなんておかしい。",
    "薬を続けていると、考えが混乱しなくてすむ。",
    "薬を続けていれば、病気の予防になる",
]

INSTRUMENTS: Dict[str, Dict[str, Any]] = {
    "bdi2": {
        "code": "bdi2",
        "name": "日本版 BDI-II",
        "instructions": "【回答の方法】\n「この質問票には21の項目があります。それぞれの項目に含まれる文章をひとつひとつ注意深く読み、それぞれの項目で、今日を含むこの2週間のあなたの気持ちに最も近い文章をひとつ選び、選んだ文章の番号を○で囲んでください。\nもし、ひとつの項目で同じように当てはまる文章がいくつかある場合は、番号の大きい方を○で囲んでください。No.16 (睡眠習慣の変化)やNo.18 (食欲の変化)も含め、それぞれの項目で必ずひとつだけ選んでください。」",
        "questions": BDI2_QUESTIONS,
    },
    "sds": {
        "code": "sds",
        "name": "SDS (Zung Self-Rating Depression Scale)",
        "instructions": "【回答の方法】\n「おもての質問を読んで現在あなたの状態にもっともよくあてはまると思われる欄に印をつけてください。」",
        "questions": SDS_QUESTIONS,
    },
    "sassj": {
        "code": "sassj",
        "name": "SASS-J (社会適応自己評価尺度)",
        "instructions": "【回答の方法】\n「以下の質問に対して自分にあてはまるものを選び、その( )の中に○をつけてください。」",
        "questions": SASSJ_QUESTIONS,
    },
    "phq9": {
        "code": "phq9",
        "name": "PHQ-9 日本語版",
        "instructions": "「この2週間、次のような問題にどのくらい頻繁(ひんぱん)に悩まされていますか？」\n「右の欄の最もよくあてはまる選択肢の中から一つ選び、その数字に○をつけてください。」",
        "questions": PHQ9_QUESTIONS,
    },
    "stai_x1": {
        "code": "stai_x1",
        "name": "日本版 STAI 状態不安 (X-1)",
        "instructions": "やり方①\n「下に文章が並んでいますから、読んで、この質問紙を記入している今現在のあなたの気持ちをよく表すように、それぞれの文の右の欄に○をつけてください。」\n「(選択肢: 全くちがう / いくらか / まあそうだ / その通りだ)」",
        "questions": [
            {"key": f"q{i+1}", "text": txt, "options": STAI_X1_OPTIONS}
            for i, txt in enumerate(STAI_X1_QUESTIONS)
        ],
    },
    "stai_x2": {
        "code": "stai_x2",
        "name": "日本版 STAI 特性不安 (X-2)",
        "instructions": "やり方②\n「下に文章が並んでいますから、読んで、今度はあなたのふだんの気持ちをよく表すように、それぞれの文の右の欄に○をつけてください。」\n「(選択肢: ほとんどない / ときたま / しばしば / しょっちゅう)」",
        "questions": [
            {"key": f"q{i+21}", "text": txt, "options": STAI_X2_OPTIONS}
            for i, txt in enumerate(STAI_X2_QUESTIONS)
        ],
    },
    "dai10": {
        "code": "dai10",
        "name": "薬に対するアンケート (DAI-10)",
        "instructions": "「現在、ご自身が飲んでいる薬に対する印象を聞くアンケートです。各項目で、あてはまる方に○をつけて下さい」",
        "questions": [
            {
                "key": f"q{i+1}",
                "text": txt,
                "options": DAI10_OPTIONS,
                "reverse": (i + 1) in DAI10_REVERSE_ITEMS,
                "max_score": 1,
            }
            for i, txt in enumerate(DAI10_QUESTIONS)
        ],
    },
}


def get_instrument(code: str) -> Dict[str, Any]:
    return INSTRUMENTS[code]


def instrument_label(code: str) -> str:
    return INSTRUMENTS.get(code, {}).get("name", code)


def next_instrument(code: str) -> str | None:
    try:
        idx = INSTRUMENT_ORDER.index(code)
    except ValueError:
        return None
    if idx + 1 < len(INSTRUMENT_ORDER):
        return INSTRUMENT_ORDER[idx + 1]
    return None


def prev_instrument(code: str) -> str | None:
    try:
        idx = INSTRUMENT_ORDER.index(code)
    except ValueError:
        return None
    if idx - 1 >= 0:
        return INSTRUMENT_ORDER[idx - 1]
    return None


def _score_for_question(question: Dict[str, Any], answer_id: Any) -> int:
    for opt in question.get("options", []):
        if str(opt.get("id")) == str(answer_id):
            score = int(opt.get("score", 0))
            if question.get("reverse"):
                max_score = int(question.get("max_score", max(o.get("score", 0) for o in question.get("options", [])) or 0))
                return max_score + 1 - score
            return score
    return 0


def calculate_score(code: str, answers: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    meta = INSTRUMENTS.get(code)
    if not meta:
        return 0, {}
    total = 0
    extras: Dict[str, Any] = {}
    for q in meta.get("questions", []):
        key = q.get("key")
        if key is None:
            continue
        if key not in answers:
            continue
        score = _score_for_question(q, answers.get(key))
        if code == "phq9" and key == "q10":
            extras["phq9_q10"] = score
            continue
        if q.get("include_in_total", True):
            total += score
    return total, extras


__all__ = [
    "INSTRUMENTS",
    "INSTRUMENT_ORDER",
    "get_instrument",
    "instrument_label",
    "next_instrument",
    "prev_instrument",
    "calculate_score",
]
