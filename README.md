# eca-digital-youtube-kids

Coletor em Python (YouTube Data API v3) e base de dados de vídeos infantis em português, para investigar **sinais observáveis de risco relacionados ao ECA Digital** (Lei nº 15.211/2025).

Projeto da disciplina **CCF427 –  ANÁLISE E MINERAÇÃO DE MÍDIAS SOCIAIS**.

**Autores:** Ana Clara Oliveira Soares, Gabriel Vitor Teixeira Santos, Maria Eduarda Duarte Lacerda

---

## Contexto

O ECA Digital entrou em vigor em 17/03/2026 e impõe deveres às plataformas digitais acessíveis a crianças. Este projeto coleta vídeos, canais e comentários de conteúdo infantil publicado no YouTube **depois** da vigência da lei, para caracterizar o que circula e identificar sinais de risco observáveis por meio de metadados e comentários.

**Importante:** os dados coletados **não permitem afirmar que um vídeo viola a lei**. Não temos acesso ao conteúdo audiovisual analisado, à monetização, aos anúncios exibidos nem ao perfilamento feito pela plataforma. O trabalho fala em *indícios* e *sinais*, não em violações. Veja a seção [Limitações](#limitações).

## Questão de pesquisa

> Que sinais observáveis em metadados e comentários de vídeos infantis no YouTube brasileiro sugerem riscos relacionados ao ECA Digital?

## Como os dados foram coletados

Não existe API pública do YouTube Kids. Usamos a **YouTube Data API v3** e identificamos conteúdo infantil pelo campo `status.madeForKids` (declarado pelo criador ou definido pelo YouTube) além de uma busca direcionada usando palavras chave relacionadas ao público infantil.

| Etapa | Chamada da API | O que coleta | Custo de cota |
|---|---|---|---|
| 1 | `search.list` | IDs de vídeos por palavra-chave e janela mensal | 100 |
| 2 | `videos.list` | Metadados, estatísticas e `madeForKids` | 1 |
| 3 | `channels.list` | Metadados dos canais | 1 |
| 4 | `commentThreads.list` | Comentários e respostas (onde habilitados) | 1 |
| 5 | — | Exportação para CSV | — |

**Parâmetros de busca:** `type=video`, `regionCode=BR`, `relevanceLanguage=pt`, `safeSearch=none`, `order=relevance`, 50 resultados por consulta e janela.

**Consultas** (arquivo `busca.txt`): 15 palavras-chave em português, como *desenho animado infantil*, *cantigas infantis*, *unboxing brinquedos*, *pegadinhas infantis* e *youtuber mirim*.

**Janelas:** agosto, setembro e outubro de 2026. Coleta realizada em 30/10/2026.

O script salva tudo em um banco SQLite e pode ser interrompido e retomado sem repetir buscas já feitas, o que permite respeitar a cota diária da API.

## Volume

| Tabela | Registros |
|---|---|
| Buscas (`search_hits`) | 2.250 |
| Vídeos (`videos`) | 1.937 |
| Canais (`channels`) | 1.064 |
| Comentários (`comments`) | 33.370 |

Dos vídeos, 1.030 estão marcados como feitos para crianças (`made_for_kids=1`). Todos os comentários vêm de vídeos **sem** esse rótulo, pois os marcados como infantis têm comentários desativados.

## Estrutura do repositório

```
.
├── dados_exportados
├── testes_iniciais
├── coletor_youtube_kids.py   # script de coleta
├── busca.txt                 # palavras-chave (uma por linha)
├── README.md
└── .gitignore
```

Os dados (`youtube_kids.db` e `dados_exportados/`) **não** são versionados, por conterem texto de comentários.

## Como reproduzir

**1. Chave da API.** No [Google Cloud Console](https://console.cloud.google.com), crie um projeto, ative a **YouTube Data API v3** e gere uma chave de API. Nunca publique a chave no repositório.

**2. Dependência:**

```bash
pip install google-api-python-client
```

**3. Chave no ambiente:**

```bash
# Mac/Linux
export YOUTUBE_API_KEY="sua_chave"

# Windows (PowerShell)
$env:YOUTUBE_API_KEY="sua_chave"
```

**4. Execução:**

```bash
python coletor_youtube_kids.py --queries-file busca.txt --months 3 --max-pages 1
```


Opções principais:

| Parâmetro | Descrição | Padrão |
|---|---|---|
| `--queries-file` | arquivo `.txt` com uma palavra-chave por linha (obrigatório) | — |
| `--months` | quantos meses para trás | 6 |
| `--max-pages` | páginas de busca por consulta (50 vídeos/página) | 2 |
| `--region` / `--lang` | região e idioma de preferência | `BR` / `pt` |
| `--safe-search` | `none`, `moderate` ou `strict` | `none` |
| `--quota-limit` | limite de cota a gastar na execução | 9500 |
| `--comment-pages` | páginas de comentários por vídeo (100/página) | 3 |
| `--full-replies` | busca todas as respostas dos comentários | desligado |
| `--skip-search` | pula a busca e só detalha o que já foi achado | desligado |

Se a cota diária acabar, o script salva o que já coletou. Rode o mesmo comando no dia seguinte para continuar.

**Saída:** banco `youtube_kids.db` e CSVs em `dados_exportados/` (`videos.csv`, `channels.csv`, `comments.csv`, `search_hits.csv`), além de um resumo da coleta no terminal.

## Esquema dos dados

- **videos:** `video_id`, `channel_id`, `title`, `description`, `published_at`, `tags`, `category_id`, `default_language`, `default_audio_language`, `duration`, `definition`, `caption`, `view_count`, `like_count`, `comment_count`, `made_for_kids`, `comments_status`, `thumbnail_url`, entre outros.
- **channels:** `channel_id`, `title`, `description`, `country`, `published_at`, `subscriber_count`, `video_count`, `view_count`, `made_for_kids`.
- **comments:** `comment_id`, `video_id`, `parent_id` (vazio se for comentário de primeiro nível), `author_hash`, `text`, `like_count`, `published_at`, `updated_at`.
- **search_hits:** `video_id`, `query`, `window_start`, `window_end`, `rank`.

## Limitações

1. **Amostra por palavras-chave, não aleatória.** Só entram vídeos que a busca do YouTube devolveu para 15 termos. A ordenação por relevância favorece vídeos populares.
2. **Limite de resultados por busca.** São 50 vídeos por consulta e mês, apenas um recorte do universo.
3. **`madeForKids` é autodeclarado.** Há vídeos claramente infantis sem o rótulo, e o inverso.
4. **Sem comentários em vídeos marcados como infantis.** Isso impede análises de interação nesse grupo.
5. **Recorte curto:** três meses, todos após a vigência da lei, sem comparação com o período anterior.
6. **Idioma e região.** `relevanceLanguage=pt` é preferência de ranking, não filtro rígido; há vídeos em outros idiomas. O idioma declarado pelo criador é pouco confiável.
7. **Dados que a API não fornece:** conteúdo audiovisual, monetização, anúncios exibidos, perfilamento e recomendação.
8. **Retrato de um único dia:** visualizações e curtidas mudam com o tempo.
9. **Anomalia a verificar:** há comentários com data anterior à janela de publicação dos vídeos. [CONFIRMAR a causa]
10. **Indícios não são prova de violação.** A caracterização jurídica não pode ser feita só com dados públicos.

## Ética e privacidade

- Os IDs dos autores de comentários são **pseudonimizados** com hash (SHA-256 truncado) antes de serem salvos.
- Os dados são públicos e foram coletados pela API oficial, dentro dos termos de uso e da cota.
- Recomenda-se não divulgar texto bruto de comentários nem identificar canais de menores nos resultados.

## Licença

Código sob licença MIT. Veja o arquivo LICENSE.
