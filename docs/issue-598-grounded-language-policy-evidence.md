# Issue #598: Grounded language policy evidence

## Evidence boundary

This document records the retained evidence for the language-aware grounded source policies.
The policy file is `configs/grounded-source-policies.json`.
This document is a record of what that file held at issue #598.
It is not a statement about the file today: issue #643 gives an editor a command that accepts a proposed policy, which raises the count and can promote one entry to verified.
At issue #598 the file held 52 policy objects across 41 canonical hosts.
The canonical host counts were `en` 28, `de` 4, `fr` 2, `es` 3, `pt` 2, `it` 1, and `cs` 1.
Every policy had `verification_status: "unverified"` at that time.
The evidence below does not promote any policy to verified status.
No live network request was made for this document.
The URLs are retained URLs from existing feasibility documents and Candidate History records.

## Retained policy rows

| Language | Canonical host | Narrow path prefix | Owner | Status |
|---|---|---|---|---|
| `es` | `todostuslibros.com` | `/libros/` | `publisher-supplied-copy` | `unverified` |
| `es` | `planetadelibros.com.ar` | `/libro-` | `publisher-supplied-copy` | `unverified` |
| `es` | `lanacion.com.ar` | `/politica/` | `la-nacion-editorial` | `unverified` |
| `pt` | `sextante.com.br` | `/products/` | `publisher-supplied-copy` | `unverified` |
| `pt` | `veja.abril.com.br` | `/vitrine-livros-mais-vendidos/` | `veja-editorial` | `unverified` |
| `it` | `premiostrega.it` | `/PSS/libro/` | `premio-strega-editorial` | `unverified` |
| `cs` | `magnesia-litera.cz` | `/kniha/` | `magnesia-litera-editorial` | `unverified` |

## Prefix rationale

- `todostuslibros.com/libros/` selects individual TodosTusLibros book pages and excludes the general site, category pages, and other catalogue routes.
- `planetadelibros.com.ar/libro-` selects Planeta book product pages because the retained product URLs use this literal prefix and other site pages do not qualify as book evidence.
- `lanacion.com.ar/politica/` selects the retained book article surface while excluding unrelated La Nación news sections.
- `sextante.com.br/products/` selects publisher product pages and leaves the grounding gate to reject product pages without matching title, author, and subject evidence.
- `veja.abril.com.br/vitrine-livros-mais-vendidos/` selects the retained Veja book-list surface and excludes other Veja editorial pages.
- `premiostrega.it/PSS/libro/` selects individual Premio Strega book pages and excludes the award list page and unrelated site routes.
- `magnesia-litera.cz/kniha/` selects individual Magnesia Litera book pages and excludes the award homepage and non-book categories.

All seven prefixes are intentionally narrow.
The classifier also removes at most one leading locale segment from a closed allowlist before it applies a prefix.
The classifier does not widen a prefix to the host root.

## Retained source URLs

The source URLs below are the exact URLs retained in Candidate History or the cited feasibility records.
They are evidence references only and are not network instructions.

| Language | Source surface | Exact retained source URL |
|---|---|---|
| `es` | TodosTusLibros recommendation module | `https://www.todostuslibros.com/libros/atlas-mitologico-de-grecia_979-13-87933-06-7` |
| `es` | Planeta product page | `https://www.planetadelibros.com.ar/libro-la-soledad/419707` |
| `es` | La Nación politics book coverage | `https://www.lanacion.com.ar/politica/solo-en-off-firmenich-se-confiesa-en-un-libro-y-le-toma-examen-a-sus-exsoldados-de-montoneros-nid17082026/` |
| `pt` | Sextante product page | `https://sextante.com.br/products/a-morte-que-vale-a-pena-viver` |
| `pt` | Veja book bestseller surface | `https://veja.abril.com.br/vitrine-livros-mais-vendidos/o-poder-da-autorresponsabilidade-2/` |
| `it` | Premio Strega Saggistica 2026 cinquina | `https://premiostrega.it/PSS/i-cinque-libri-candidati-al-premio-strega-saggistica-2edizione/` |
| `cs` | Magnesia Litera 2026 homepage | `https://www.magnesia-litera.cz/` |

## Candidate History evidence

The entries below preserve the exact candidate identity and evidence URL retained in `runs/current-weekly-pilot-russian/candidate-history.json`.
The records are historical evidence and do not claim a new live check.

### Spanish

| Source | Candidate title | Candidate author | Exact evidence URL |
|---|---|---|---|
| TodosTusLibros | `Atlas mitológico de Grecia` | `Pedro Olalla` | `https://www.todostuslibros.com/libros/atlas-mitologico-de-grecia_979-13-87933-06-7` |
| TodosTusLibros | `Cuando el mundo duerme` | `Francesca Albanese` | `https://www.todostuslibros.com/libros/cuando-el-mundo-duerme_979-13-88019-05-0` |
| TodosTusLibros | `El final de Israel` | `ILAN PAPPE` | `https://www.todostuslibros.com/libros/el-final-de-israel_978-84-460-5795-6` |
| TodosTusLibros | `Historia de las sensibilidades` | `Alain Corbin` | `https://www.todostuslibros.com/libros/historia-de-las-sensibilidades_979-13-87964-29-0` |
| TodosTusLibros | `Historias de fantasmas` | `Siri Hustvedt` | `https://www.todostuslibros.com/libros/historias-de-fantasmas_978-84-322-4980-8` |
| TodosTusLibros | `Irreductibles` | `Kate Manne` | `https://www.todostuslibros.com/libros/irreductibles_979-13-991058-3-4` |
| TodosTusLibros | `La fosa abierta` | `Brigitte Vasallo` | `https://www.todostuslibros.com/libros/la-fosa-abierta_978-84-339-4871-7` |
| TodosTusLibros | `Los lugares de Petrarca` | `Eduardo Antonio Prieto González` | `https://www.todostuslibros.com/libros/los-lugares-de-petrarca_979-13-87964-16-0` |
| TodosTusLibros | `Marisabidillas, frívolas y peligrosas` | `Rocío González Naranjo` | `https://www.todostuslibros.com/libros/marisabidillas-frivolas-y-peligrosas_979-13-87554-31-6` |
| TodosTusLibros | `Me crie como un fascista` | `Antonio Maestre` | `https://www.todostuslibros.com/libros/me-crie-como-un-fascista_978-84-322-4966-2` |
| TodosTusLibros | `Oxígeno` | `Marta Jiménez Serrano` | `https://www.todostuslibros.com/libros/oxigeno_978-84-204-5399-6` |
| TodosTusLibros | `Paseos con Robert Walser` | `SEELIG CARL` | `https://www.todostuslibros.com/libros/paseos-con-robert-walser_979-13-87688-76-9` |
| TodosTusLibros | `Redimir y adoctrinar` | `Carmen Guillén Lorente` | `https://www.todostuslibros.com/libros/redimir-y-adoctrinar_978-84-9199-828-0` |
| TodosTusLibros | `Sobrevivir en el Medievo` | `Mikel Herrán` | `https://www.todostuslibros.com/libros/sobrevivir-en-el-medievo_978-84-08-32009-8` |
| TodosTusLibros | `Tres amigas` | `Jesús Marchamalo` | `https://www.todostuslibros.com/libros/tres-amigas_979-13-87922-34-4` |
| TodosTusLibros | `Un deseo desmesurado de amistad` | `Hélène Giannecchini` | `https://www.todostuslibros.com/libros/un-deseo-desmesurado-de-amistad_978-84-339-4894-6` |
| TodosTusLibros | `Un metro cuadrado` | `Llucia Ramis` | `https://www.todostuslibros.com/libros/un-metro-cuadrado_978-84-10178-61-8` |
| TodosTusLibros | `Una Woolf pròpia` | `Ali Smith` | `https://www.todostuslibros.com/libros/una-woolf-propia_978-84-10487-52-9` |
| TodosTusLibros | `Y de todo mal me sana un buen verso` | `Fabio Stassi` | `https://www.todostuslibros.com/libros/y-de-todo-mal-me-sana-un-buen-verso_979-13-87964-10-8` |
| Planeta | `LA SOLEDAD` | `ROLON, GABRIEL` | `https://www.planetadelibros.com.ar/libro-la-soledad/419707` |
| Planeta | `MONTONEROS` | `O'DONNELL, MARIA` | `https://www.planetadelibros.com.ar/libro-montoneros/414819?soporte=450225` |
| La Nación | `ORIGENES` | `FIRMENICH, MARIO EDUARDO` | `https://www.lanacion.com.ar/politica/solo-en-off-firmenich-se-confiesa-en-un-libro-y-le-toma-examen-a-sus-exsoldados-de-montoneros-nid17082026/` |

### Portuguese

| Source | Candidate title | Candidate author | Exact evidence URL |
|---|---|---|---|
| Sextante | `A morte é um dia que vale a pena viver` | `Ana Claudia Quintana Arantes` | `https://sextante.com.br/products/a-morte-que-vale-a-pena-viver` |
| Sextante | `As coisas que você só vê quando desacelera` | `Haemin Sunim` | `https://sextante.com.br/products/as-coisas-que-voce-so-ve-quando-desacelera` |
| Sextante | `Pai, me conta a sua história?` | `Elma Van Vliet` | `https://sextante.com.br/products/pai-me-conta-a-sua-historia` |
| Sextante | `Stop Letting Everything Affect You` | `Daniel Chidiac` | `https://sextante.com.br/products/nao-deixe-tudo-te-abalar` |
| Sextante | `Vertigem` | `Lela Brandão` | `https://sextante.com.br/products/vertigem` |
| Veja | `O poder da autorresponsabilidade` | `Paulo Vieira` | `https://veja.abril.com.br/vitrine-livros-mais-vendidos/o-poder-da-autorresponsabilidade-2/` |

### Italian

| Source | Candidate title | Candidate author | Exact evidence URL |
|---|---|---|---|
| Premio Strega | `Alfabit. L’italiano digitale dagli SMS all’IA` | `Giuseppe Antonelli` | `https://premiostrega.it/PSS/libro/alfabit-litaliano-digitale-dagli-sms-allia/` |
| Premio Strega | `L’omicidio di Piersanti Mattarella. L’Italia nel mirino: Palermo, Ustica, Bologna. 1979-1980` | `Miguel Gotor` | `https://premiostrega.it/PSS/libro/lomicidio-di-piersanti-mattarella/` |
| Premio Strega | `La peste. Indagine sulla destra in Germania` | `Tonia Mastrobuoni` | `https://premiostrega.it/PSS/libro/la-peste-indagine-sulla-destra-in-germania/` |
| Premio Strega | `Vertigine. Storie di chi si affida alla scienza e di chi impara a farlo` | `Beatrice Mautino` | `https://premiostrega.it/PSS/libro/vertigine-storie-di-chi-si-affida-alla-scienza-e-di-chi-impara-a-farlo/` |
| Premio Strega | `Oriente. Una storia` | `Alessandro Vanoli` | `https://premiostrega.it/PSS/libro/oriente-una-storia/` |

### Czech

| Source | Candidate title | Candidate author | Exact evidence URL |
|---|---|---|---|
| Magnesia Litera | `Bohové a bohyně starých Slovanů` | `Jiří Dynda` | `https://www.magnesia-litera.cz/kniha/bohove-a-bohyne-starych-slovanu/` |
| Magnesia Litera | `Dneska večer potkáš sovu` | `Martin Tomáš` | `https://www.magnesia-litera.cz/kniha/dneska-vecer-potkas-sovu/` |
| Magnesia Litera | `Fatální kniha` | `Jana Kantoříková` | `https://www.magnesia-litera.cz/kniha/fatalni-kniha/` |
| Magnesia Litera | `Kacířské eseje o budoucnosti svobody a demokracie` | `Václav Němec` | `https://www.magnesia-litera.cz/kniha/kacirske-eseje-o-budoucnosti-svobody-a-demokracie/` |
| Magnesia Litera | `Křesťanství ve víru kulturních válek. Papežství, hodnoty, identita` | `Tomáš Petráček` | `https://www.magnesia-litera.cz/kniha/krestanstvi-ve-viru-kulturnich-valek-papezstvi-hodnoty-identita/` |
| Magnesia Litera | `Labyrint bolesti` | `Pavel Kolář` | `https://www.magnesia-litera.cz/kniha/labyrint-bolesti/` |
| Magnesia Litera | `Periferní průvodce Prahou` | `Tomáš Staněk` | `https://www.magnesia-litera.cz/kniha/periferni-pruvodce-prahou/` |
| Magnesia Litera | `Ruská duše neexistuje` | `Jan Bělíček; Tomáš Glanc` | `https://www.magnesia-litera.cz/kniha/ruska-duse-neexistuje/` |
| Magnesia Litera | `Slunce, voda, vzduch` | `Jan Pezda` | `https://www.magnesia-litera.cz/kniha/slunce-voda-vzduch/` |
| Magnesia Litera | `Toyen. Léta erotická` | `Andrea Sedláčková` | `https://www.magnesia-litera.cz/kniha/toyen-leta-eroticka/` |
| Magnesia Litera | `V pasti české pomoci` | `Saša Uhlová` | `https://www.magnesia-litera.cz/kniha/v-pasti-ceske-pomoci/` |
| Magnesia Litera | `České zbraně pro Ukrajinu` | `Ondřej Kundra; Tomáš Brolík` | `https://www.magnesia-litera.cz/kniha/ceske-zbrane-pro-ukrajinu/` |

## Feasibility document citations

- `docs/issue-290-spanish-source-feasibility.md` records the retained TodosTusLibros recommendation surface, its exact book-link examples, its mixed-list limitation, and its `unverified` policy boundary.
- `docs/issue-291-italian-source-feasibility.md` records the five linked Premio Strega shortlist books and the edition-specific list URL.
- `docs/issue-296-czechia-source-feasibility.md` records the two qualifying nonfiction categories and the linked Czech book examples.
- `docs/issue-562-brazil-source-feasibility.md` records the Portuguese Brazilian source feasibility and the replay-first probation boundary for the related market work.
- `docs/issue-572-korea-source-feasibility.md` records the blocked Korean Aladin source and explains why no `ko` policy row is present.

The Spanish Planeta and La Nación rows, and the Portuguese Sextante and Veja rows, rely on the exact retained Candidate History URLs listed above rather than a new feasibility claim.
The Portuguese rows remain `unverified` until a separate policy-focused feasibility check is completed.

## Independence boundary

The Spanish TodosTusLibros and Planeta rows use the same owner `publisher-supplied-copy`.
The Portuguese Sextante product URLs also use the same owner `publisher-supplied-copy`.
Two URLs from either shared-owner set can ground at most one accepted source in the universal gate.
They cannot corroborate independently, even when their URLs and hosts differ.
The Spanish La Nación row uses `la-nacion-editorial`, and the Portuguese Veja row uses `veja-editorial`.
Those editorial owners remain distinct from `publisher-supplied-copy`.
The regression tests in `tests/test_issue_598_grounded_language_policy.py` exercise both shared-owner sets through `evaluate_grounding_gate`.

Italian and Czech each have only one admitted owner.
This is insufficient for the two-independent-owner grounding gate.
This records a deliberate deferral of a second owner because the retained repository has no observed subject-bearing URL from a distinct owner.
The Italian and Czech policy rows therefore do not claim full two-owner coverage.
Norwegian is also a deliberate deferral.
The retained evidence contains 24 Norwegian candidates and 19 distinct observed Norwegian hosts.
Zero hosts were admitted under any policy language, and therefore zero admitted owners.
This is explicit visibility, not a proven outcome regression.
A live search could in principle find a future admissible host.
All new policy entries remain `unverified`.

## Search locale decision

The decision is to send Brave `search_lang` but omit `country`.
Source language is reliable candidate metadata.
Source market or country is not a safe search-country constraint and can differ from the evidence location.

## Operational conclusion

The language metadata is bounded, lowercase, and validated as ISO 639-1.
The loopback adapter passes only validated lowercase `search_lang` values to Brave.
Invalid `search_lang` values are omitted from the Brave request and translated result metadata.
Conflicting valid explicit candidate language fields fail closed without fallback to `primary_signal`.
A known language with no admissible policy host produces `no_admissible_host_for_language` before discovery.
No policy row is verified by this document.
