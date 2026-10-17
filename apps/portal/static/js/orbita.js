/* Órbita — o único JavaScript próprio do portal.
 *
 * São ~70 linhas, e isso é a consequência de uma escolha, não um acidente:
 * tudo que normalmente exigiria JavaScript (busca que atualiza a lista, log ao
 * vivo, tabela que se recarrega, formulário que volta com erro) é feito com
 * HTMX e atributos no HTML. O que sobra aqui é o que o HTMX não cobre:
 *
 *   1. alternar o tema claro/escuro e lembrar a escolha;
 *   2. rolar o log para o fim quando chegam linhas novas;
 *   3. avisar quando a conexão falha, em vez de o clique parecer ignorado.
 *
 * Sem bundler, sem framework, sem etapa de build.
 */

(function () {
  "use strict";

  /* ── 1. Tema ─────────────────────────────────────────────────────────────
   * Três estados, não dois: "claro", "escuro" e **ausente**. Ausente significa
   * "siga o sistema", que é o padrão e o que a maioria quer. Um alternador de
   * dois estados obriga a pessoa a escolher e nunca mais volta a acompanhar o
   * sistema.
   */
  const CHAVE_TEMA = "orbita-tema";

  function temaDoSistema() {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "escuro" : "claro";
  }

  function alternarTema() {
    const raiz = document.documentElement;
    const atual = raiz.dataset.tema || temaDoSistema();
    const novo = atual === "escuro" ? "claro" : "escuro";

    raiz.dataset.tema = novo;
    try {
      // Se o novo tema coincide com o do sistema, remove a preferência em vez
      // de gravá-la: assim a página volta a acompanhar o sistema quando ele
      // mudar (por exemplo, o modo escuro automático à noite).
      if (novo === temaDoSistema()) {
        localStorage.removeItem(CHAVE_TEMA);
        delete raiz.dataset.tema;
      } else {
        localStorage.setItem(CHAVE_TEMA, novo);
      }
    } catch (e) {
      /* localStorage bloqueado (janela privada, cookies desligados): o tema
         vale para esta navegação e não é lembrado. Melhor do que não alternar. */
    }
  }

  document.addEventListener("click", function (evento) {
    const botao = evento.target.closest("[data-alternar-tema]");
    if (botao) alternarTema();
  });

  /* ── 2. Rolagem do log ───────────────────────────────────────────────────
   * Só rola se a pessoa já estava no fim. Rolar sempre arrancaria a tela do
   * lugar no momento em que ela subiu para ler uma linha de erro — e é
   * exatamente quando ela mais precisa que a tela fique quieta.
   */
  const MARGEM_DO_FIM = 80;

  function estaNoFim(elemento) {
    return (
      elemento.scrollHeight - elemento.scrollTop - elemento.clientHeight < MARGEM_DO_FIM
    );
  }

  document.body.addEventListener("htmx:beforeSwap", function (evento) {
    const log = evento.detail.target.closest(".log") || evento.detail.target;
    if (log && log.classList.contains("log")) {
      log.dataset.estavaNoFim = estaNoFim(log) ? "1" : "0";
    }
  });

  document.body.addEventListener("htmx:afterSwap", function (evento) {
    const log = evento.detail.target.closest(".log") || evento.detail.target;
    if (log && log.classList.contains("log") && log.dataset.estavaNoFim !== "0") {
      log.scrollTop = log.scrollHeight;
    }
  });

  /* ── 3. Falha de rede ────────────────────────────────────────────────────
   * Sem isto, uma requisição HTMX que falha não produz nenhum efeito visível:
   * o botão parece simplesmente não funcionar. O aviso é discreto e some
   * sozinho — o caso mais comum é o serviço hibernando no free tier, que
   * responde no segundo clique.
   */
  document.body.addEventListener("htmx:sendError", mostrarFalhaDeRede);
  document.body.addEventListener("htmx:timeout", mostrarFalhaDeRede);

  let avisoVisivel = null;

  function mostrarFalhaDeRede() {
    if (avisoVisivel) return;

    const aviso = document.createElement("div");
    aviso.className = "alerta alerta--aviso";
    aviso.setAttribute("role", "status");
    aviso.innerHTML =
      "<div class='alerta__corpo'><p class='alerta__titulo sem-margem'>Sem resposta do servidor</p>" +
      "<p class='sem-margem pequeno'>A conexão falhou ou o serviço está iniciando. Tente novamente em alguns segundos.</p></div>";

    const conteudo = document.getElementById("conteudo");
    if (conteudo) conteudo.prepend(aviso);
    avisoVisivel = aviso;

    setTimeout(function () {
      aviso.remove();
      avisoVisivel = null;
    }, 6000);
  }
})();
