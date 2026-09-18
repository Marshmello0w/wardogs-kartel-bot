
window.WDRCON = window.WDRCON || {};

(function () {
	"use strict";

	class HttpApiClient {
		constructor() {
			this._base = null;
			this._key = null;

			this._experienceNames = null;
		}

		features = {
			changeTeam: false,
			reservedExpiry: false,
			serverId: false,

			configDocument: false,
		};

		async probeCapabilities() {
			if (this._capabilities) { return this._capabilities; }
			const data = await this._fetch("GET", "/v1/capabilities");
			this._capabilities = data;

			const norm = (route) => String(route).trim()
				.replace(/\{[^}]*\}|:[^/\s]+/g, "*")
				.replace(/\s+/g, " ");
			const routes = (data.routes || []).map(norm);
			this.features.changeTeam = routes.includes("PATCH /v1/players/*");
			this.features.serverId = routes.includes("GET /v1/server-id");

			this.features.configDocument =
				routes.includes("PUT /v1/config") && !!(data.config && data.config.writable);
			return data;
		}

		async connect({ host, port, password }) {
			this._base = `http://${host}:${Number(port)}`;
			this._key = password;
			const status = await this._fetch("GET", "/v1/status");
			return { serverName: status.serverName };
		}

		async disconnect() {
			this._key = null;
		}

		async getStatus() {
			const s = await this._fetch("GET", "/v1/status");
			const rotation = s.rotation || {};
			this._factions = s.factionScores || [];
			return {
				serverName: s.serverName,
				map: s.map,
				experiences: s.experiences || [],
				lighting: s.lighting,
				alternator: s.alternator || "",
				scoreTick: s.scoreTick.current,
				scoreTickMin: s.scoreTick.min,
				scoreTickMax: s.scoreTick.max,
				scoreCap: s.scoreCap,
				matchSeconds: s.matchSeconds,
				playerCount: s.players.current,
				maxPlayers: s.players.max,
				scores: s.factionScores || [],
				rotationNow: rotation.nowIndex === null || rotation.nowIndex === undefined ? -1 : rotation.nowIndex,
				rotationNext: rotation.nextIndex === null || rotation.nextIndex === undefined ? -1 : rotation.nextIndex,
			};
		}

		async getPlayers() {
			const data = await this._fetch("GET", "/v1/players");
			const players = (data.players || []).map((p) => ({
				name: p.name,
				steamId: p.steamId,
				faction: p.faction,
				kills: p.kills,
				deaths: p.deaths,
				cash: p.cash,
				ping: p.pingMs,
			}));
			return this._withProfiles(players);
		}

		async _withProfiles(rows) {
			let profiles = {};
			try {
				profiles = await window.WDRCON.steam.profiles(rows.map((r) => r.steamId));
			} catch {  }
			return rows.map((row) => ({
				...row,
				steamName: (profiles[row.steamId] && profiles[row.steamId].name) || null,
				avatarUrl: (profiles[row.steamId] && profiles[row.steamId].avatar) || null,
			}));
		}

		kickPlayer(steamId, reason) {
			return this._fetch("POST", `/v1/players/${encodeURIComponent(steamId)}/kick`,
				{ reason: reason || "Kicked by admin." });
		}

		banPlayer(steamId, reason) {
			return this._fetch("POST", "/v1/bans", { steamId, reason: reason || undefined });
		}

		killPlayer(steamId) {
			return this._fetch("POST", `/v1/players/${encodeURIComponent(steamId)}/kill`);
		}

		async changeTeam(steamId, faction) {
			if (!faction) { throw new Error("Pick a faction to move the player to."); }
			const moved = await this._fetch("PATCH", `/v1/players/${encodeURIComponent(steamId)}`, { faction });

			let respawned = true;
			try {
				await this.killPlayer(steamId);
			} catch {
				respawned = false;
			}
			return { ...moved, respawned };
		}

		async getFactions() {
			if (!this._factions) { await this.getStatus(); }
			return (this._factions || [])
				.filter((f) => f && f.name)
				.map((f) => ({ name: f.name, colorHex: f.colorHex || "" }));
		}

		messagePlayer(steamId, text) {
			return this._fetch("POST", `/v1/players/${encodeURIComponent(steamId)}/message`, { message: text });
		}

		broadcast(text) {
			return this._fetch("POST", "/v1/broadcast", { message: text });
		}

		async getMaps() {
			const data = await this._fetch("GET", "/v1/catalog/maps");
			return (data.maps || []).map((m) => ({ id: m.id, display: m.displayName || m.id }));
		}

		async getLightings() {
			const data = await this._fetch("GET", "/v1/catalog/lightings");
			return (data.lightings || []).map((l) => ({ id: l.id, display: l.displayName || l.id }));
		}

		async getExperiences(mapId) {
			if (!mapId) {
				const data = await this._fetch("GET", "/v1/catalog/experiences");
				const all = (data.experiences || []).map((e) => ({ id: e.id, display: e.displayName || e.id }));
				this._experienceNames = new Map(all.map((e) => [e.id, e.display]));
				return all;
			}
			if (!this._experienceNames) { await this.getExperiences(); }
			const data = await this._fetch("GET", `/v1/catalog/maps/${encodeURIComponent(mapId)}/experiences`);
			return (data.experiences || []).map((id) => ({ id, display: this._experienceNames.get(id) || id }));
		}

		async getAlternators(mapId) {
			const data = await this._fetch("GET", `/v1/catalog/maps/${encodeURIComponent(mapId)}/alternators`);
			return (data.alternators || []).map((a) => ({ tag: a.tag, display: a.displayName || a.tag }));
		}

		changeMap({ map, experiences, lighting, alternator }) {
			return this._fetch("POST", "/v1/match/map", mapSelection(map, experiences, lighting, alternator));
		}

		setWeather(lighting) {
			return this._fetch("PUT", "/v1/world/lighting", { lighting });
		}

		endMatch() {
			return this._fetch("POST", "/v1/match/end");
		}

		restartMatch() {
			return this._fetch("POST", "/v1/match/restart");
		}

		async setNextMap(selection) {
			const { nowIndex: now } = await this.getRotation();
			return this._editRotation((entries) => {
				let index = entries.findIndex((raw, i) => i !== now && sameSelection(parseRotationEntry(raw), selection));
				let nowAfter = now;
				if (index < 0) {
					entries.push(formatRotationEntry(selection));
					index = entries.length - 1;
				} else if (now > index) {
					nowAfter = now - 1;
				}
				const restLast = entries.length - 2;
				const slot = nowAfter < 0 || nowAfter >= restLast ? 0 : nowAfter + 1;
				if (index !== slot) {
					const [moved] = entries.splice(index, 1);
					entries.splice(slot, 0, moved);
				}
				return `Next map set to ${selection.map} (rotation entry ${slot + 1}).`;
			});
		}

		async getRotation() {
			const r = await this._fetch("GET", "/v1/rotation");
			const entries = r.entries || [];
			return {
				enabled: !!r.enabled,
				mode: r.mode || "ordered",
				nowIndex: entries.findIndex((e) => e.status === "now"),
				nextIndex: entries.findIndex((e) => e.status === "next"),
				entries: entries.map((e) => ({
					map: e.map,
					experiences: e.experiences || [],
					lighting: e.lighting || "",
					alternator: e.zoneAlternator || "None",
					denied: !!e.denied,
				})),
			};
		}

		addRotationEntry(selection) {
			return this._editRotation((entries) => {
				entries.push(formatRotationEntry(selection));
				return `Added ${selection.map} as rotation entry ${entries.length}.`;
			});
		}

		removeRotationEntry(index) {
			return this._editRotation((entries) => {
				if (index < 0 || index >= entries.length) { throw new Error(`No rotation entry ${index + 1}.`); }
				entries.splice(index, 1);
				return `Removed rotation entry ${index + 1}.`;
			});
		}

		moveRotationEntry(index, direction) {
			return this.reorderRotationEntry(index, direction === "up" ? index - 1 : index + 1);
		}

		reorderRotationEntry(index, to) {
			return this._editRotation((entries) => {
				if (index < 0 || index >= entries.length || to < 0 || to >= entries.length) {
					throw new Error(`Cannot move rotation entry ${index + 1} to position ${to + 1}.`);
				}
				const [moved] = entries.splice(index, 1);
				entries.splice(to, 0, moved);
				return `Moved rotation entry ${index + 1} to position ${to + 1}.`;
			});
		}

		setSettings(patch) {
			const writes = [];
			for (const [key, value] of Object.entries(patch)) {
				if (key === "scoretick") { writes.push([KOTH_SECTION, "ScorePeriod", String(Number(value))]); }
				else if (key === "rotationenabled") { writes.push([ROTATION_SECTION, "bEnabled", value === true || value === "on" ? "True" : "False"]); }
				else if (key === "rotationmode") { writes.push([ROTATION_SECTION, "RotationMode", String(value).toLowerCase() === "random" ? "Random" : "Ordered"]); }
				else { return Promise.reject(new Error(`Unknown setting '${key}'.`)); }
			}
			if (!writes.length) { return Promise.reject(new Error("No settings to apply.")); }
			return this._editConfig((doc) => {
				for (const [section, ini, v] of writes) { doc.setScalar(section, ini, v); }
				return `Applied ${writes.map((w) => w[1]).join(", ")}.`;
			});
		}

		setSetting(key, value) {
			return this.setSettings({ [key]: value });
		}

		async getBans() {
			const data = await this._fetch("GET", "/v1/bans");
			const bans = (data.bans || []).map((b) => ({
				steamId: b.steamId,
				bannedAtUtc: b.bannedAtUtc || "-",
				bannedBy: b.bannedBy || "-",
				reason: b.reason || "-",
			}));
			return this._withProfiles(bans);
		}

		unban(steamId) {
			return this._fetch("DELETE", `/v1/bans/${encodeURIComponent(steamId)}`);
		}

		async getReserved() {
			const data = await this._fetch("GET", "/v1/reserved-slots");
			const ids = (data.reservedSlots || []).map((steamId) => ({ steamId, expiresAtUtc: null }));
			return this._withProfiles(ids);
		}

		addReserved(steamId, durationDays) {
			if (durationDays) {
				return Promise.reject(new Error("This server's reservations are permanent-only; remove the slot to revoke it."));
			}
			return this._editConfig((doc) => {
				const ids = doc.getArray(GAME_SESSION_SECTION, "DefaultReservedPlayerIds") || [];
				if (ids.some((id) => bareId(id) === steamId)) { throw new Error(`SteamId ${steamId} is already reserved.`); }
				doc.setArray(GAME_SESSION_SECTION, "DefaultReservedPlayerIds", [...ids, steamId]);
				return `Reserved a slot for ${steamId}.`;
			});
		}

		removeReserved(steamId) {
			return this._editConfig((doc) => {
				const ids = doc.getArray(GAME_SESSION_SECTION, "DefaultReservedPlayerIds") || [];
				const kept = ids.filter((id) => bareId(id) !== steamId);
				if (kept.length === ids.length) { throw new Error(`SteamId ${steamId} has no reserved slot.`); }
				doc.setArray(GAME_SESSION_SECTION, "DefaultReservedPlayerIds", kept);
				return `Removed the reserved slot for ${steamId}.`;
			});
		}

		resolveSteamProfiles(steamIds) {
			return window.WDRCON.steam.profiles(steamIds);
		}

		async getSponsor() {
			const data = await this._fetch("GET", "/v1/sponsor");
			return { url: data.imageUrl || "" };
		}

		async getAudit(tail) {
			const limit = Math.min(500, Math.max(1, Number(tail) || 50));
			const data = await this._fetch("GET", `/v1/audit?limit=${limit}`);
			return (data.entries || []).map((e) => ({
				timestampUtc: e.timestampUtc,
				peer: e.peer,
				sessionId: e.sessionId,
				event: e.event,
				detail: e.detail || "",
			}));
		}

		async getServerId() {
			const data = await this._fetch("GET", "/v1/server-id");
			return String(data.serverId || "");
		}

		async _editConfig(mutate) {
			const live = await this.getConfig();
			const doc = window.WDRCON.ConfigDoc.parse(live.text).doc;
			const message = mutate(doc);
			const result = await this.applyConfig(doc.serialise(), { revision: live.revision });
			if (!result.ok) {
				const detail = result.errors.length
					? result.errors.map((e) => e.message || `${e.key}: ${e.code}`).join(" ")
					: result.errorMessage;
				throw new Error(result.conflict ? `The server's config changed underneath this edit; try again. ${detail}`.trim() : detail);
			}
			const outcome = result.outcomes.map((o) => o.detail).filter(Boolean)[0];
			return { message: outcome ? `${message} ${outcome}` : message };
		}

		_editRotation(mutate) {
			return this._editConfig((doc) => {
				const entries = doc.getArray(ROTATION_SECTION, "RotationEntries") || [];
				const message = mutate(entries);
				doc.setArray(ROTATION_SECTION, "RotationEntries", entries);
				return message;
			});
		}

		async getConfig() {
			const data = await this._fetch("GET", "/v1/config");
			return {
				revision: data.revision || "",
				writable: data.writable !== false,
				text: data.text || "",
				sections: data.sections || [],
				warnings: data.warnings || [],
			};
		}

		validateConfig(text) {
			return this._configResult("POST", "/v1/config/validate", text);
		}

		applyConfig(text, { revision, force, fullApply } = {}) {
			const query = [];
			if (force) { query.push("force=true"); }
			if (fullApply) { query.push("fullApply=true"); }
			return this._configResult("PUT", "/v1/config" + (query.length ? `?${query.join("&")}` : ""), text, revision);
		}

		async _configResult(method, path, text, revision) {
			const headers = { "Authorization": `Bearer ${this._key}`, "Content-Type": "text/plain" };
			if (revision) { headers["If-Match"] = `"${revision}"`; }

			let res;
			try {
				res = await fetch(this._base + path, { method, headers, body: text, cache: "no-store", credentials: "omit" });
			} catch {
				throw new Error("Could not reach the server. Check the host and port, that the RCON listener is enabled, and that this page is loaded over HTTP (an HTTPS page cannot call the plain-HTTP server API).");
			}

			const raw = await res.text();
			let body = {};
			if (raw) { try { body = JSON.parse(raw); } catch { body = {}; } }

			return {
				ok: res.ok && body.ok !== false,
				status: res.status,
				conflict: res.status === 412,
				revision: body.revision || "",
				errorCode: (body.error && body.error.code) || "",
				errorMessage: (body.error && body.error.message) || (res.ok ? "" : `Request failed (${res.status}).`),
				outcomes: body.outcomes || [],
				shadowed: body.shadowed || [],
				stripped: body.stripped || [],
				errors: body.errors || [],
				changed: body.changed || [],
				conflictDeltas: body.conflict || [],
				warnings: body.warnings || [],
				timingsMs: body.timingsMs || null,
			};
		}

		async _fetch(method, path, body) {
			const headers = { "Authorization": `Bearer ${this._key}` };
			if (body !== undefined) { headers["Content-Type"] = "application/json"; }

			let res;
			try {
				res = await fetch(this._base + path, {
					method,
					headers,
					body: body === undefined ? undefined : JSON.stringify(body),
					cache: "no-store",

					credentials: "omit",
				});
			} catch {

				throw new Error("Could not reach the server. Check the host and port, that the RCON listener is enabled, and that this page is loaded over HTTP (an HTTPS page cannot call the plain-HTTP server API).");
			}

			const text = await res.text();
			let parsed = null;
			if (text) {
				try { parsed = JSON.parse(text); } catch {  }
			}

			if (!res.ok) {
				throw new Error((parsed && parsed.error && parsed.error.message) || `Request failed (${res.status}).`);
			}
			return parsed || {};
		}
	}

	const GAME_SESSION_SECTION = "/Script/WDGame.WDGameSession";
	const ROTATION_SECTION = "/Script/WDGame.WDServerMapRotationSettings";
	const KOTH_SECTION = "MatchState.Playing.KOTH";

	const bareId = (value) => String(value).trim().replace(/^"|"$/g, "");

	function parseRotationEntry(raw) {
		const entry = { map: "", experiences: [], lighting: "", alternator: "None" };
		const inner = String(raw).trim().replace(/^\(|\)$/g, "");
		for (const m of inner.matchAll(/(\w+)=(?:"([^"]*)"|([^,]*))/g)) {
			const value = (m[2] !== undefined ? m[2] : m[3] || "").trim();
			if (m[1] === "Map") { entry.map = value; }
			else if (m[1] === "Experience" && value) { entry.experiences.push(value); }
			else if (m[1] === "Experiences" && value) { entry.experiences.push(...value.split("+").filter(Boolean)); }
			else if (m[1] === "Lighting") { entry.lighting = value; }
			else if (m[1] === "ZoneAlternator") { entry.alternator = value || "None"; }
		}
		return entry;
	}

	function formatRotationEntry({ map, experiences, lighting, alternator }) {
		const q = (value) => '"' + String(value).replace(/"/g, "") + '"';
		const parts = [`Map=${q(map)}`];
		const ids = (experiences || []).filter(Boolean);
		if (ids.length === 1) { parts.push(`Experience=${q(ids[0])}`); }
		else if (ids.length > 1) { parts.push(`Experiences=${q(ids.join("+"))}`); }
		if (lighting) { parts.push(`Lighting=${q(lighting)}`); }
		if (alternator && alternator !== "None") { parts.push(`ZoneAlternator=${q(alternator)}`); }
		return `(${parts.join(",")})`;
	}

	function mapSelection(map, experiences, lighting, alternator) {
		const body = { map };
		if (experiences && experiences.length) { body.experiences = experiences; }
		if (lighting) { body.lighting = lighting; }
		if (alternator && alternator !== "None") { body.zoneAlternator = alternator; }
		return body;
	}

	function sameSelection(entry, selection) {
		const key = (value) => (!value || value === "None" ? "" : String(value));
		const set = (ids) => [...(ids || [])].sort().join("+");
		return entry.map === selection.map &&
			set(entry.experiences) === set(selection.experiences) &&
			key(entry.lighting) === key(selection.lighting) &&
			key(entry.alternator) === key(selection.alternator);
	}

	function testTarget({ host, port }) {
		const config = window.WDRCON.CONFIG;
		const list = config.testServers ||
			[{ ...config.testLogin, data: "TEST_DATA", label: "Test Server" }];
		const same = (a, b) => String(a === undefined ? "" : a).trim().toLowerCase() === String(b).trim().toLowerCase();
		return list.find((t) => same(host, t.host) && same(port, t.port)) || null;
	}

	function createClient({ host, port, password }) {
		const test = testTarget({ host, port });

		if (test && (!test.password || String(password) === String(test.password))) {
			return new window.WDRCON.MockApiClient(window.WDRCON[test.data] || window.WDRCON.TEST_DATA);
		}
		return new HttpApiClient();
	}

	window.WDRCON.HttpApiClient = HttpApiClient;
	window.WDRCON.createClient = createClient;
	window.WDRCON.testTarget = testTarget;
})();
