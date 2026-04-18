/* ============================================================
   Nosite — logique frontend
   HTML/CSS/JS vanilla. Compatible file:// et GitHub Pages.
   ============================================================ */

(() => {
  'use strict';

  // --------------------------------------------------------
  // Config
  // --------------------------------------------------------

  const CONFIG = {
    NICE_CENTER: [43.7102, 7.2620],
    NICE_ZOOM: 12,
    // DÉCISION: tuiles Carto Positron (light_all) pour matcher le design clair premium
    TILE_URL: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    TILE_ATTR: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    STORAGE_KEY: 'nosite_user_data_v1',
    FETCH_TIMEOUT_MS: 10000,   // 10 s pour télécharger le Gist
    FETCH_RETRIES: 1,          // 1 retry après échec réseau
    FETCH_RETRY_DELAY_MS: 1000,
    CONFIG_PLACEHOLDER: 'A_REMPLACER_APRES_CREATION_GIST',
    // Score max observé sur le dataset (utilisé pour la barre dans le tooltip)
    SCORE_MAX: 174,
  };

  // Types d'erreur pour affichage différencié
  const ERREURS = {
    CONFIG_MANQUANTE: 'config_manquante',
    CONFIG_PLACEHOLDER: 'config_placeholder',
    CONFIG_URL_INVALIDE: 'config_url_invalide',
    RESEAU: 'reseau',
    HTTP: 'http',
    FORMAT: 'format',
  };

  // DÉCISION: un seul ordre d'affichage pour les classifications, utilisé
  // à la fois dans les filtres et dans le tri de la liste.
  const CLASSIFICATIONS = ['TRÈS PROBABLE', 'PROBABLE', 'À VÉRIFIER'];

  const CLASSIF_KEY = {
    'TRÈS PROBABLE': 'tres-probable',
    'PROBABLE': 'probable',
    'À VÉRIFIER': 'a-verifier',
    'ÉCARTÉ': 'ecarte',
  };

  // --------------------------------------------------------
  // State global
  // --------------------------------------------------------

  const state = {
    payload: null,
    entreprises: [],
    filters: {
      search: '',
      classifications: new Set(CLASSIFICATIONS), // toutes par défaut
      naf: new Set(),                            // rempli après chargement
      hideContacted: false,
    },
    userData: {},            // { [siren]: { contacte, date_contact, notes } }
    selectedSiren: null,
    visibleSirens: new Set(),
    map: null,
    cluster: null,
    markersBySiren: new Map(),
    listItemsBySiren: new Map(), // référence DOM pour re-rendu rapide
  };

  // --------------------------------------------------------
  // Utilitaires
  // --------------------------------------------------------

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function normaliser(txt) {
    return (txt || '')
      .toString()
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .toLowerCase();
  }

  function formatEffectif(tranche) {
    const libelles = {
      '01': '1-2 sal.', '02': '3-5 sal.', '03': '6-9 sal.',
      '11': '10-19 sal.', '12': '20-49 sal.', '21': '50-99 sal.',
      '22': '100-199 sal.', '31': '200-249 sal.', '32': '250-499 sal.',
    };
    return libelles[tranche] || tranche || '—';
  }

  function formatDate(iso) {
    if (!iso) return '—';
    // iso peut être "YYYY-MM-DD" ou ISO complet
    try {
      const d = new Date(iso);
      if (isNaN(d)) return iso;
      return d.toLocaleDateString('fr-FR', { day: '2-digit', month: 'short', year: 'numeric' });
    } catch (_) { return iso; }
  }

  function debounce(fn, delay) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), delay);
    };
  }

  function escapeHtml(s) {
    return (s || '')
      .toString()
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // --------------------------------------------------------
  // localStorage
  // --------------------------------------------------------

  function chargerUserData() {
    try {
      const raw = localStorage.getItem(CONFIG.STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (_) {
      return {};
    }
  }

  function sauverUserData() {
    try {
      localStorage.setItem(CONFIG.STORAGE_KEY, JSON.stringify(state.userData));
    } catch (_) {
      // DÉCISION: si localStorage est indisponible (mode privé Safari, quota…),
      // on n'avertit pas l'utilisateur — ses données en mémoire restent actives
      // pour la session en cours. Documenter si on voit des rapports terrain.
    }
  }

  function getUser(siren) {
    return state.userData[siren] || { contacte: false, date_contact: null, notes: '' };
  }

  function setUser(siren, patch) {
    const current = getUser(siren);
    state.userData[siren] = { ...current, ...patch };
    sauverUserData();
  }

  // --------------------------------------------------------
  // Chargement des données
  // --------------------------------------------------------

  class NositeError extends Error {
    constructor(code, message, details) {
      super(message);
      this.code = code;
      this.details = details || {};
    }
  }

  function verifierConfig() {
    const cfg = window.NOSITE_CONFIG;
    if (!cfg) {
      throw new NositeError(
        ERREURS.CONFIG_MANQUANTE,
        "config.js est introuvable ou ne définit pas window.NOSITE_CONFIG."
      );
    }
    const url = (cfg.dataUrl || '').trim();
    if (!url) {
      throw new NositeError(
        ERREURS.CONFIG_MANQUANTE,
        "La propriété dataUrl est vide dans config.js."
      );
    }
    if (url === CONFIG.CONFIG_PLACEHOLDER) {
      throw new NositeError(
        ERREURS.CONFIG_PLACEHOLDER,
        "L'URL du Gist n'a pas encore été renseignée dans config.js."
      );
    }
    if (!/^https?:\/\//i.test(url)) {
      throw new NositeError(
        ERREURS.CONFIG_URL_INVALIDE,
        `L'URL configurée ne commence pas par http(s):// — reçu : ${url}`
      );
    }
    return url;
  }

  // Fetch avec timeout via AbortController
  async function fetchAvecTimeout(url, timeoutMs) {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeoutMs);
    try {
      // DÉCISION: cache: 'no-store' pour toujours avoir la dernière version
      // du Gist (ils sont servis avec des headers de cache parfois longs).
      return await fetch(url, { signal: controller.signal, cache: 'no-store' });
    } finally {
      clearTimeout(id);
    }
  }

  async function fetchAvecRetry(url) {
    let derniereErreur;
    for (let i = 0; i <= CONFIG.FETCH_RETRIES; i++) {
      try {
        const res = await fetchAvecTimeout(url, CONFIG.FETCH_TIMEOUT_MS);
        if (!res.ok) {
          throw new NositeError(
            ERREURS.HTTP,
            `Le serveur a répondu HTTP ${res.status} ${res.statusText}`,
            { status: res.status }
          );
        }
        return res;
      } catch (err) {
        derniereErreur = err;
        // Ne pas retry les erreurs HTTP applicatives (404, 401, etc.)
        if (err instanceof NositeError && err.code === ERREURS.HTTP) break;
        if (i < CONFIG.FETCH_RETRIES) {
          await new Promise(r => setTimeout(r, CONFIG.FETCH_RETRY_DELAY_MS));
        }
      }
    }
    if (derniereErreur instanceof NositeError) throw derniereErreur;
    // Erreur réseau / timeout / CORS
    throw new NositeError(
      ERREURS.RESEAU,
      `Impossible de joindre l'URL configurée : ${derniereErreur?.message || 'erreur réseau'}`
    );
  }

  async function chargerDonnees() {
    // DÉCISION: si data.js local est présent (dev : quelqu'un a lancé
    // `python src/main.py` sans pousser les data dans un Gist), on les
    // utilise en priorité. Ça permet de développer l'UI sans Gist.
    if (window.__NOSITE_DATA) {
      console.info('[nosite] données chargées depuis window.__NOSITE_DATA (mode dev local)');
      return window.__NOSITE_DATA;
    }

    // Sinon : fetch depuis l'URL configurée dans config.js (mode prod/Gist)
    const url = verifierConfig();
    const res = await fetchAvecRetry(url);

    let payload;
    try {
      payload = await res.json();
    } catch (_) {
      throw new NositeError(
        ERREURS.FORMAT,
        "L'URL a répondu mais le contenu n'est pas du JSON valide."
      );
    }
    return payload;
  }

  // --------------------------------------------------------
  // Construction de la sidebar (filtres)
  // --------------------------------------------------------

  function construireFiltres() {
    // Classification : boutons radio stylés
    const container = $('#filter-classification');
    container.innerHTML = '';
    const options = [{ value: '__all', label: 'Toutes' }, ...CLASSIFICATIONS.map(c => ({ value: c, label: c }))];
    options.forEach((opt, i) => {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'radio';
      input.name = 'classification';
      input.value = opt.value;
      if (i === 0) { input.checked = true; label.classList.add('checked'); }
      input.addEventListener('change', () => {
        $$('#filter-classification label').forEach(l => l.classList.remove('checked'));
        label.classList.add('checked');
        if (opt.value === '__all') {
          state.filters.classifications = new Set(CLASSIFICATIONS);
        } else {
          state.filters.classifications = new Set([opt.value]);
        }
        appliquerFiltres();
      });
      label.appendChild(input);
      label.appendChild(document.createTextNode(' ' + opt.label));
      container.appendChild(label);
    });

    // NAF : checkboxes basées sur naf_presents du payload
    const nafBox = $('#filter-naf');
    nafBox.innerHTML = '';
    const nafs = state.payload.naf_presents || [];
    nafs.forEach(({ code, libelle }) => {
      state.filters.naf.add(code);
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.value = code;
      input.checked = true;
      input.addEventListener('change', () => {
        if (input.checked) state.filters.naf.add(code);
        else state.filters.naf.delete(code);
        appliquerFiltres();
      });
      const codeSpan = document.createElement('span');
      codeSpan.className = 'naf-code';
      codeSpan.textContent = code;
      const libelleSpan = document.createElement('span');
      libelleSpan.className = 'naf-libelle';
      libelleSpan.textContent = libelle;
      libelleSpan.title = libelle;
      label.append(input, codeSpan, libelleSpan);
      nafBox.appendChild(label);
    });
  }

  // --------------------------------------------------------
  // Init carte
  // --------------------------------------------------------

  function initCarte() {
    state.map = L.map('map', { zoomControl: true, preferCanvas: true })
      .setView(CONFIG.NICE_CENTER, CONFIG.NICE_ZOOM);

    L.tileLayer(CONFIG.TILE_URL, {
      attribution: CONFIG.TILE_ATTR,
      maxZoom: 19,
      subdomains: 'abcd',
    }).addTo(state.map);

    // Cluster custom (fond blanc, bordure noire, taille dynamique)
    state.cluster = L.markerClusterGroup({
      showCoverageOnHover: false,
      maxClusterRadius: 48,
      iconCreateFunction: (cluster) => {
        const n = cluster.getChildCount();
        const size = n < 10 ? 32 : n < 100 ? 40 : 48;
        return L.divIcon({
          html: `<div class="nosite-cluster" style="width:${size}px;height:${size}px">${n}</div>`,
          className: 'nosite-cluster-wrapper',
          iconSize: [size, size],
        });
      },
    });

    state.map.addLayer(state.cluster);

    // Construire tous les markers une fois ; visibilité pilotée par filtres
    state.entreprises.forEach(ent => {
      if (ent.lat == null || ent.lng == null) return; // skip si pas de géoloc
      const user = getUser(ent.siren);
      const marker = L.marker([ent.lat, ent.lng], {
        icon: creerIcone(ent.classification, false, user.contacte),
        riseOnHover: true,
        // DÉCISION: on garde les markers cliquables même à l'intérieur d'un
        // cluster — markercluster par défaut le fait via spiderfy (éclatement
        // au clic sur le cluster), on ne désactive pas cette fonctionnalité.
      });
      // Tooltip hover — c'est LE moment "wow" du produit : nom + NAF + barre de score
      marker.bindTooltip(buildTooltipHTML(ent), {
        direction: 'top',
        offset: [0, -8],
        className: 'nosite-tooltip',
        opacity: 1,
      });
      // Clic marker → ouvre direct le panel détaillé (plus de popup intermédiaire)
      marker.on('click', () => {
        selectionner(ent.siren, { centrer: false, ouvrirFiche: true });
      });
      marker._nosite_ent = ent;
      state.markersBySiren.set(ent.siren, marker);
    });
  }

  // DÉCISION: trois tailles de marker selon la classification pour hiérarchiser
  // visuellement la carte. Les "contactées" reçoivent un style dédié (pointillé
  // + opacité réduite) au lieu d'être masquées — elles restent visibles mais
  // en retrait.
  function creerIcone(classification, selected, contacted) {
    const size = classification === 'TRÈS PROBABLE' ? 14 : 12;
    const cls = CLASSIF_KEY[classification] || 'a-verifier';
    const modifiers = [
      `nosite-marker--${cls}`,
      contacted ? 'is-contacted' : '',
      selected ? 'is-selected' : '',
    ].filter(Boolean).join(' ');
    return L.divIcon({
      className: 'nosite-marker-wrapper',
      html: `<div class="nosite-marker ${modifiers}" style="width:${size}px;height:${size}px"></div>`,
      iconSize: [size, size],
      iconAnchor: [size / 2, size / 2],
    });
  }

  // Rafraîchit l'icône d'un marker pour refléter l'état courant (contacté, sélectionné).
  // Utilisé à chaque fois que l'utilisateur coche/décoche "contactée".
  function rafraichirMarker(siren) {
    const marker = state.markersBySiren.get(siren);
    const ent = state.entreprises.find(e => e.siren === siren);
    if (!marker || !ent) return;
    const selected = state.selectedSiren === siren;
    marker.setIcon(creerIcone(ent.classification, selected, getUser(siren).contacte));
  }

  // Tooltip custom — inspiration Cobe Globe : nom + NAF + barre de score colorée
  function buildTooltipHTML(ent) {
    const pct = Math.min(100, Math.round((ent.score / CONFIG.SCORE_MAX) * 100));
    const color = ent.classification === 'TRÈS PROBABLE' ? '#16A34A'
                : ent.classification === 'PROBABLE' ? '#CA8A04'
                : '#737373';
    // Ceinture de sécurité : si une entrée est dans la base avec un site
    // détecté (V2), on le signale dans le tooltip même si elle est visible.
    const siteLine = ent.site?.detected_url
      ? `<div class="nosite-tooltip-note">⚠️ Site détecté (à vérifier)</div>`
      : '';
    return `
      <div class="nosite-tooltip-inner">
        <div class="nosite-tooltip-naf">${escapeHtml(ent.naf?.libelle || '')}</div>
        <div class="nosite-tooltip-name">${escapeHtml(ent.nom)}</div>
        <div class="nosite-tooltip-score-row">
          <div class="nosite-tooltip-bar">
            <div class="nosite-tooltip-bar-fill" style="width: ${pct}%; background: ${color};"></div>
          </div>
          <div class="nosite-tooltip-score">${ent.score}pt</div>
        </div>
        ${siteLine}
      </div>
    `;
  }

  // --------------------------------------------------------
  // Liste (construction puis filtrage)
  // --------------------------------------------------------

  function construireListeComplete() {
    const list = $('#list');
    list.innerHTML = '';
    state.listItemsBySiren.clear();

    // Les entreprises sont déjà triées par score desc dans data.js
    state.entreprises.forEach(ent => {
      const li = document.createElement('li');
      li.className = 'list-item';
      li.dataset.siren = ent.siren;

      const check = document.createElement('label');
      check.className = 'item-check';
      const checkInput = document.createElement('input');
      checkInput.type = 'checkbox';
      checkInput.title = 'Marquer comme contacté';
      checkInput.checked = getUser(ent.siren).contacte;
      checkInput.addEventListener('click', (e) => e.stopPropagation());
      checkInput.addEventListener('change', () => {
        const contacte = checkInput.checked;
        setUser(ent.siren, {
          contacte,
          date_contact: contacte ? new Date().toISOString() : getUser(ent.siren).date_contact,
        });
        li.classList.toggle('contacted', contacte);
        rafraichirMarker(ent.siren);
        if (state.selectedSiren === ent.siren) rendreFiche(ent);
        if (state.filters.hideContacted) appliquerFiltres();
      });
      check.appendChild(checkInput);

      const body = document.createElement('div');
      body.className = 'item-body';

      const nom = document.createElement('div');
      nom.className = 'item-name';
      nom.textContent = ent.nom;

      const meta = document.createElement('div');
      meta.className = 'item-meta';
      const badge = document.createElement('span');
      badge.className = `badge badge-${CLASSIF_KEY[ent.classification]}`;
      badge.textContent = ent.classification;
      meta.appendChild(badge);
      const site = document.createElement('span');
      site.textContent = `${ent.cp || ''} ${ent.ville || ''} · ${formatEffectif(ent.effectif_tranche)}`;
      meta.appendChild(site);

      body.append(nom, meta);
      li.append(check, body);

      if (getUser(ent.siren).contacte) li.classList.add('contacted');

      li.addEventListener('click', () => {
        selectionner(ent.siren, { centrer: true, ouvrirFiche: true });
      });

      list.appendChild(li);
      state.listItemsBySiren.set(ent.siren, li);
    });
  }

  function appliquerFiltres() {
    const q = normaliser(state.filters.search);
    state.visibleSirens.clear();

    let visibles = 0;
    state.entreprises.forEach(ent => {
      const user = getUser(ent.siren);
      const classifOk = state.filters.classifications.has(ent.classification);
      const nafOk = !ent.naf?.code || state.filters.naf.has(ent.naf.code);
      const searchOk = !q
        || normaliser(ent.nom).includes(q)
        || normaliser(ent.sigle).includes(q)
        || normaliser(ent.adresse).includes(q);
      const contactOk = !(state.filters.hideContacted && user.contacte);

      const visible = classifOk && nafOk && searchOk && contactOk;
      const li = state.listItemsBySiren.get(ent.siren);
      if (li) li.classList.toggle('hidden', !visible);

      const marker = state.markersBySiren.get(ent.siren);
      if (marker) {
        if (visible) {
          state.visibleSirens.add(ent.siren);
          if (!state.cluster.hasLayer(marker)) state.cluster.addLayer(marker);
        } else {
          if (state.cluster.hasLayer(marker)) state.cluster.removeLayer(marker);
        }
      }
      if (visible) visibles++;
    });

    $('#compteur-visible').textContent = visibles;
    $('#compteur-total').textContent = state.entreprises.length;

    const list = $('#list');
    let empty = list.querySelector('.list-empty');
    if (visibles === 0) {
      if (!empty) {
        empty = document.createElement('li');
        empty.className = 'list-empty';
        empty.innerHTML = '<strong>Aucun résultat</strong><span>Essayez de relâcher les filtres ou la recherche.</span>';
        list.appendChild(empty);
      }
      empty.classList.remove('hidden');
    } else if (empty) {
      empty.classList.add('hidden');
    }
  }

  // --------------------------------------------------------
  // Sélection (liste ↔ carte ↔ fiche)
  // --------------------------------------------------------

  function selectionner(siren, { centrer = false, ouvrirFiche = false } = {}) {
    const ent = state.entreprises.find(e => e.siren === siren);
    if (!ent) return;

    // Déselection précédente
    if (state.selectedSiren && state.selectedSiren !== siren) {
      const prevEnt = state.entreprises.find(e => e.siren === state.selectedSiren);
      const prev = state.markersBySiren.get(state.selectedSiren);
      if (prev && prevEnt) {
        prev.setIcon(creerIcone(prevEnt.classification, false, getUser(prevEnt.siren).contacte));
      }
      const prevLi = state.listItemsBySiren.get(state.selectedSiren);
      if (prevLi) prevLi.classList.remove('active');
    }

    state.selectedSiren = siren;
    const li = state.listItemsBySiren.get(siren);
    if (li) {
      li.classList.add('active');
      // Scroll doux dans la liste
      li.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    const marker = state.markersBySiren.get(siren);
    if (marker) {
      marker.setIcon(creerIcone(ent.classification, true, getUser(siren).contacte));
      if (centrer && ent.lat != null && ent.lng != null) {
        // Si le marker est dans un cluster, zoom dessus pour l'afficher
        state.cluster.zoomToShowLayer(marker, () => {
          state.map.setView([ent.lat, ent.lng], Math.max(state.map.getZoom(), 15), { animate: true });
        });
      }
    }

    if (ouvrirFiche) ouvrirFichePanel(ent);
  }

  // --------------------------------------------------------
  // Fiche détaillée (panel)
  // --------------------------------------------------------

  function ouvrirFichePanel(ent) {
    rendreFiche(ent);
    $('#detail-panel').classList.add('open');
    $('#detail-backdrop').classList.add('open');
    $('#detail-panel').setAttribute('aria-hidden', 'false');
  }

  function fermerFichePanel() {
    $('#detail-panel').classList.remove('open');
    $('#detail-backdrop').classList.remove('open');
    $('#detail-panel').setAttribute('aria-hidden', 'true');
  }

  // Section "DÉTECTION SITE WEB" (V2 Serper). Fallback DNS (V1) si pas de `site`.
  function renderSiteSection(ent) {
    const site = ent.site;
    if (!site) {
      // Mode legacy DNS : on affiche la section DNS si disponible, sinon rien.
      return ent.dns ? renderDnsSection(ent) : '';
    }

    const hasSite = !!site.has_site;
    const icon = hasSite ? '🌐' : '🚫';
    const ligneStatut = hasSite
      ? `<strong>Site détecté :</strong> <a href="${escapeHtml(site.detected_url || '')}" target="_blank" rel="noopener noreferrer">${escapeHtml(site.detected_domain || site.detected_url || '')}</a>`
      : `<strong>Aucun site web détecté</strong> — prospect qualifié.`;
    const ligneRaison = site.reason
      ? `<div class="site-reason">${escapeHtml(site.reason)}</div>`
      : '';
    const ligneQuery = site.query_used
      ? `<div class="site-query">Requête Google : <code>${escapeHtml(site.query_used)}</code></div>`
      : '';

    return `
      <div class="detail-section detail-site">
        <h3>Détection site web</h3>
        <div class="site-verdict ${hasSite ? 'site-has' : 'site-none'}">
          ${icon} ${ligneStatut}
        </div>
        ${ligneRaison}
        ${ligneQuery}
      </div>
    `;
  }

  // Legacy : section "SIGNAUX DNS" conservée pour le mode --legacy-dns.
  function renderDnsSection(ent) {
    const dnsResolus = (ent.dns?.domaines_resolus || []).map(d =>
      `<span class="chip ok">${escapeHtml(d)}</span>`
    ).join('');
    const dnsKo = (ent.dns?.domaines_testes || [])
      .filter(d => !(ent.dns?.domaines_resolus || []).includes(d))
      .map(d => `<span class="chip ko">${escapeHtml(d)}</span>`)
      .join('');

    return `
      <div class="detail-section">
        <h3>Signaux DNS</h3>
        <div class="dns-list">
          ${dnsResolus || '<span style="color:var(--text-muted);font-size:12px">Aucun domaine résolu — probable absence de site web.</span>'}
        </div>
        ${dnsKo ? `
          <div style="margin-top:10px">
            <div style="font-size:11px;color:var(--text-dim);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px">Variantes testées</div>
            <div class="dns-list">${dnsKo}</div>
          </div>
        ` : ''}
      </div>
    `;
  }

  function rendreFiche(ent) {
    const content = $('#detail-content');
    const user = getUser(ent.siren);
    const p = ent.pappers || {};
    const dirigeant = p.dirigeant || (ent.dirigeants_recherche && ent.dirigeants_recherche[0]) || null;

    const telHref = p.telephone ? `tel:${p.telephone.replace(/\s+/g, '')}` : null;
    const mailHref = p.email ? `mailto:${p.email}` : null;

    const gmapsQuery = encodeURIComponent(`${ent.nom} ${ent.adresse || ''}`);
    const gmapsUrl = `https://www.google.com/maps/search/?api=1&query=${gmapsQuery}`;
    const pappersUrl = `https://www.pappers.fr/entreprise/${ent.siren}`;

    const dirigeantHtml = dirigeant
      ? `${escapeHtml(dirigeant.nom || '')}${dirigeant.qualite ? ' <span style="color:var(--text-muted)">— ' + escapeHtml(dirigeant.qualite) + '</span>' : ''}`
      : '<span style="color:var(--text-dim)">Non renseigné</span>';

    const contactDate = user.contacte && user.date_contact
      ? `<div class="detail-contact-date">✓ Contacté le ${formatDate(user.date_contact)}</div>`
      : '';

    content.innerHTML = `
      <div class="detail-header">
        <div>
          <h2 class="detail-title">${escapeHtml(ent.nom)}</h2>
          ${ent.sigle ? `<div class="detail-sigle">${escapeHtml(ent.sigle)}</div>` : ''}
        </div>
        <button class="detail-close" id="btn-close-detail" aria-label="Fermer">
          <svg viewBox="0 0 20 20" fill="none"><path d="M5 5l10 10M15 5L5 15" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        </button>
      </div>

      <div class="detail-section">
        <h3>Identité</h3>
        <dl class="detail-kv">
          <dt>Classification</dt>
          <dd><span class="badge badge-${CLASSIF_KEY[ent.classification]}">${ent.classification}</span> <span style="color:var(--text-muted)">· score ${ent.score}</span></dd>
          <dt>SIREN</dt>
          <dd>${ent.siren}</dd>
          <dt>Adresse</dt>
          <dd>${escapeHtml(ent.adresse || '—')}<br><span style="color:var(--text-muted)">${escapeHtml(ent.cp || '')} ${escapeHtml(ent.ville || '')}</span></dd>
          <dt>Activité</dt>
          <dd><strong>${escapeHtml(ent.naf?.code || '')}</strong> — ${escapeHtml(ent.naf?.libelle || '')}</dd>
          <dt>Effectif</dt>
          <dd>${formatEffectif(ent.effectif_tranche)}</dd>
          <dt>Créée le</dt>
          <dd>${formatDate(ent.date_creation)}</dd>
        </dl>
      </div>

      <div class="detail-section">
        <h3>Contact</h3>
        <dl class="detail-kv">
          <dt>Dirigeant</dt>
          <dd>${dirigeantHtml}</dd>
          <dt>Téléphone</dt>
          <dd>${telHref ? `<a href="${telHref}">${escapeHtml(p.telephone)}</a>` : '<span style="color:var(--text-dim)">Non enrichi (Pappers désactivé)</span>'}</dd>
          <dt>Email</dt>
          <dd>${mailHref ? `<a href="${mailHref}">${escapeHtml(p.email)}</a>` : '<span style="color:var(--text-dim)">Non enrichi</span>'}</dd>
        </dl>
      </div>

      ${renderSiteSection(ent)}

      <div class="detail-section">
        <h3>Actions</h3>
        <div class="detail-actions">
          <a class="detail-btn primary" href="${pappersUrl}" target="_blank" rel="noopener">Voir sur Pappers</a>
          <a class="detail-btn" href="${gmapsUrl}" target="_blank" rel="noopener">Google Maps</a>
          <button class="detail-btn ${user.contacte ? 'success' : ''}" id="btn-toggle-contact" type="button">
            ${user.contacte ? '✓ Contactée' : 'Marquer contactée'}
          </button>
        </div>
      </div>

      <div class="detail-section">
        <h3>Notes</h3>
        <textarea class="detail-notes" id="detail-notes-input" placeholder="Contexte d'appel, objections, prochaines étapes…">${escapeHtml(user.notes || '')}</textarea>
        ${contactDate}
      </div>
    `;

    // Listeners locaux
    $('#btn-close-detail').addEventListener('click', fermerFichePanel);
    $('#btn-toggle-contact').addEventListener('click', () => {
      const u = getUser(ent.siren);
      const nouveauContacte = !u.contacte;
      setUser(ent.siren, {
        contacte: nouveauContacte,
        date_contact: nouveauContacte ? new Date().toISOString() : u.date_contact,
      });
      const li = state.listItemsBySiren.get(ent.siren);
      if (li) {
        li.classList.toggle('contacted', nouveauContacte);
        const cb = li.querySelector('input[type="checkbox"]');
        if (cb) cb.checked = nouveauContacte;
      }
      rafraichirMarker(ent.siren);
      rendreFiche(ent);
      if (state.filters.hideContacted) appliquerFiltres();
    });

    const textarea = $('#detail-notes-input');
    const sauverNotes = debounce(() => setUser(ent.siren, { notes: textarea.value }), 250);
    textarea.addEventListener('input', sauverNotes);
  }

  // --------------------------------------------------------
  // Export CSV
  // --------------------------------------------------------

  function exporterCsv() {
    const colonnes = [
      'SIREN', 'Nom', 'Sigle', 'Adresse', 'CP', 'Ville',
      'NAF', 'Libellé NAF', 'Effectif', 'Date création',
      'Dirigeant', 'Qualité', 'Téléphone', 'Email',
      'Score', 'Classification', 'Contacté', 'Date contact', 'Notes',
    ];

    const visibles = state.entreprises.filter(e => state.visibleSirens.has(e.siren));
    const rows = [colonnes];

    visibles.forEach(e => {
      const u = getUser(e.siren);
      const p = e.pappers || {};
      const dirigeant = p.dirigeant || (e.dirigeants_recherche && e.dirigeants_recherche[0]) || {};
      rows.push([
        e.siren,
        e.nom || '',
        e.sigle || '',
        e.adresse || '',
        e.cp || '',
        e.ville || '',
        e.naf?.code || '',
        e.naf?.libelle || '',
        formatEffectif(e.effectif_tranche),
        e.date_creation || '',
        dirigeant.nom || '',
        dirigeant.qualite || '',
        p.telephone || '',
        p.email || '',
        e.score,
        e.classification,
        u.contacte ? 'oui' : 'non',
        u.date_contact ? formatDate(u.date_contact) : '',
        (u.notes || '').replace(/\r?\n/g, ' '),
      ]);
    });

    const csv = rows.map(row => row.map(cellule => {
      const s = (cellule == null ? '' : String(cellule));
      // Encapsuler si virgule, guillemet ou saut de ligne
      if (/[",\n;]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
      return s;
    }).join(',')).join('\n');

    const BOM = '\ufeff';
    const blob = new Blob([BOM + csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);

    const today = new Date().toISOString().slice(0, 10);
    const a = document.createElement('a');
    a.href = url;
    a.download = `nosite-nice-${today}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // --------------------------------------------------------
  // Listeners globaux
  // --------------------------------------------------------

  function brancherListeners() {
    // Recherche instantanée
    const input = $('#input-search');
    const onSearch = debounce(() => {
      state.filters.search = input.value;
      appliquerFiltres();
    }, 80);
    input.addEventListener('input', onSearch);

    // Toggle masquer contactées
    $('#toggle-hide-contacted').addEventListener('change', (e) => {
      state.filters.hideContacted = e.target.checked;
      appliquerFiltres();
    });

    // Export CSV
    $('#btn-export-csv').addEventListener('click', exporterCsv);

    // Fermer panel au clic sur le voile
    $('#detail-backdrop').addEventListener('click', fermerFichePanel);

    // Échap ferme le panel
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') fermerFichePanel();
    });
  }

  // --------------------------------------------------------
  // Entry point
  // --------------------------------------------------------

  async function init() {
    const loader = $('#loader');
    const app = $('#app');

    try {
      const payload = await chargerDonnees();
      if (!payload || !Array.isArray(payload.entreprises)) {
        throw new Error('Format de données inattendu.');
      }
      state.payload = payload;
      state.entreprises = payload.entreprises;
      state.userData = chargerUserData();

      construireFiltres();
      construireListeComplete();
      initCarte();
      brancherListeners();
      appliquerFiltres();

      loader.classList.add('fade-out');
      app.classList.remove('hidden');
      setTimeout(() => loader.classList.add('hidden'), 250);

      // Leaflet a parfois besoin d'un invalidateSize après affichage
      setTimeout(() => state.map.invalidateSize(), 350);
    } catch (err) {
      console.error('[nosite] échec init', err);
      loader.classList.add('hidden');
      afficherErreur(err);
    }
  }

  function afficherErreur(err) {
    const code = err instanceof NositeError ? err.code : null;
    let titre = 'Erreur de chargement';
    let message = err && err.message ? err.message : String(err);
    let hint = '';

    switch (code) {
      case ERREURS.CONFIG_MANQUANTE:
      case ERREURS.CONFIG_PLACEHOLDER:
      case ERREURS.CONFIG_URL_INVALIDE:
        titre = '⚠️ Configuration manquante';
        message = 'L\u2019URL du Gist privé n\u2019est pas renseignée dans public/config.js.';
        hint = 'Demande l\u2019URL à l\u2019administrateur, puis colle-la dans config.js à la place du placeholder.';
        break;

      case ERREURS.RESEAU:
        titre = '📡 Données inaccessibles';
        hint = 'Vérifie ta connexion internet et que l\u2019URL du Gist est toujours valide.';
        break;

      case ERREURS.HTTP: {
        titre = '🔒 Gist inaccessible';
        const status = err.details?.status;
        if (status === 404) {
          hint = 'Le Gist n\u2019existe plus ou a été renommé. Demande la nouvelle URL à l\u2019administrateur.';
        } else if (status === 401 || status === 403) {
          hint = 'Accès refusé au Gist. Vérifie que l\u2019URL "raw" est bien celle partagée par l\u2019administrateur.';
        } else {
          hint = 'Réessaie dans quelques secondes. Si le problème persiste, préviens l\u2019administrateur.';
        }
        break;
      }

      case ERREURS.FORMAT:
        titre = '📄 Format de données invalide';
        hint = 'Le fichier du Gist doit être le JSON produit par `python src/main.py` (public/data.json).';
        break;

      default:
        hint = 'Consulte la console du navigateur (F12) pour plus de détails.';
    }

    $('#error-title').textContent = titre;
    $('#error-message').textContent = message;
    $('#error-hint').textContent = hint;
    $('#error-banner').classList.remove('hidden');
  }

  // DOMContentLoaded n'est pas forcément déjà fired (les scripts sont en bas,
  // donc oui, mais on reste défensif)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
