# 0007 — JWT validado localmente, sem consulta ao identity-service

**Situação:** aceita · **Data:** 2026-10-17
**Implementa:** `hub_core/auth/tokens.py`

## Contexto

Quatro serviços precisam saber quem está fazendo cada requisição. Há dois
desenhos possíveis:

- **token opaco**: cada serviço consulta o `identity-service` a cada
  requisição para resolver o token. Revogação instantânea, e todo serviço
  passa a depender do identity para responder qualquer coisa;
- **token assinado (JWT)**: cada serviço valida a assinatura localmente e lê
  os claims. Sem consulta, e a revogação só vale no próximo token.

## Decisão

JWT assinado, validado localmente. Nenhum serviço consulta o
`identity-service` para autorizar uma requisição.

O token carrega o que os serviços precisam para decidir: id, nome, e-mail,
CPF, papéis, unidade e **grupos**. Os grupos entram porque o `catalog-service`
precisa deles para resolver permissão de escopo `equipe` — e consultá-los a
cada verificação recriaria exatamente a dependência síncrona que este desenho
evita.

Decisões de detalhe que importam:

- **`algorithms` é uma lista fechada com um único item.** Aceitar o algoritmo
  que o próprio token declara é a vulnerabilidade clássica de JWT: um token
  com `alg: none`, ou com `alg: HS256` quando o servidor esperava RS256 e usa
  a chave pública como segredo HMAC, passa a validar.
- **Claims específicos vão sob um namespace** (`orbita/papeis`), que é a
  convenção de Keycloak e Auth0. Onde há equivalente OIDC (`sub`, `name`,
  `email`, `iss`, `aud`, `exp`, `iat`, `jti`), o nome padrão é usado.
- **Token de serviço é assinado com outro segredo** (`INTERNAL_SHARED_SECRET`,
  não `JWT_SECRET`), para que um token de pessoa nunca sirva nos endpoints de
  callback. Alguém com token válido não deve poder declarar que uma execução
  terminou com sucesso.

## Consequências

**A favor.** O catálogo e o motor continuam atendendo com o identity fora do
ar. Nenhuma chamada de rede no caminho de autorização. Escala horizontalmente
sem estado compartilhado.

**Contra.** **Revogação não é imediata.** Tirar um papel de alguém só tem
efeito quando o token dela expira. Por isso `JWT_EXPIRE_MINUTES` é 60 e não 24
horas — e por isso o resumo de usuário guardado na sessão do portal também
pode ficar defasado, fazendo um menu aparecer por até uma hora depois de a
permissão ter sido retirada. O clique então recebe 403 do serviço, com
mensagem clara.

Para revogação instantânea seria preciso uma lista de bloqueio consultada a
cada requisição — que é exatamente a chamada síncrona que o desenho evita. O
meio-termo usual (lista de bloqueio em cache com TTL curto) traz de volta a
dependência, só que intermitente.

## Alternativas

- **Token opaco com cache.** Melhor dos dois mundos no papel; na prática, o
  cache tem TTL, e dentro do TTL a revogação também não é imediata — com a
  dependência do identity de volta.
- **Expiração de 5 minutos com refresh token.** Reduz a janela e multiplica
  por doze as chamadas ao identity, que é o que se queria evitar.
- **Keycloak desde o início.** É o alvo. Os claims já usam os nomes do OIDC,
  então a migração troca a verificação de HMAC por JWKS e o resto não muda.
