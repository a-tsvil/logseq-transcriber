(ns logseq.db-sync.worker.allowlist
  (:require [clojure.string :as string]
            [logseq.db-sync.common :as common]
            [logseq.db-sync.worker.http :as http]
            [promesa.core :as p]))

(defn normalize-email [email]
  (when (string? email)
    (let [email (string/lower-case (string/trim email))]
      (when (and (<= (count email) 254)
                 (re-matches #"[^\s@]+@[^\s@]+\.[^\s@]+" email))
        email))))

(defn- <rows [env sql & params]
  (p/let [result (apply common/<d1-all (aget env "DB") sql params)]
    (common/get-sql-rows result)))

(defn <user-identity [env user-id]
  (when (string? user-id)
    (p/let [rows (<rows env "SELECT email, email_verified FROM users WHERE id = ?" user-id)]
      (first rows))))

(defn <allowed-claims [env claims]
  (p/let [identity (when claims
                    (if (aget claims "pat_id")
                      (<user-identity env (aget claims "sub"))
                      claims))
          email (normalize-email (some-> identity (aget "email")))
          verified (some-> identity (aget "email_verified"))
          owner (normalize-email (aget env "TRAINING_OWNER_EMAIL"))
          allowed (when (and (string? (some-> claims (aget "sub")))
                             email (or (true? verified) (= 1 verified)))
                    (if (= email owner)
                      true
                      (p/let [rows (<rows env "SELECT email FROM training_allowed_accounts WHERE email = ?" email)]
                        (boolean (seq rows)))))]
    (when allowed claims)))

(defn- <close-user-sockets! [env email]
  (p/let [users (<rows env "SELECT id FROM users WHERE lower(email) = ?" email)]
    (p/all
     (for [user users]
       (p/let [user-id (aget user "id")
               graphs (<rows env "SELECT graph_id FROM graph_members WHERE user_id = ? UNION SELECT graph_id FROM graphs WHERE user_id = ?" user-id user-id)]
         (p/all
          (for [graph graphs]
            (let [^js namespace (aget env "LOGSEQ_SYNC_DO")
                  ^js stub (.get namespace (.idFromName namespace (aget graph "graph_id")))]
              (p/let [response (.fetch stub (js/Request. (str "http://internal/training/revoke?user-id=" (js/encodeURIComponent user-id))))]
                (when-not (.-ok response)
                  (throw (js/Error. "Could not close revoked sessions"))))))))))))

(defn handle [request env]
  (let [secret (aget env "TRAINING_ALLOWLIST_TOKEN")
        supplied (.get (.-headers request) "authorization")]
    (cond
      (not (and (string? secret) (>= (count secret) 64)
                (= supplied (str "Bearer " secret))))
      (http/unauthorized)

      (and (= "/training/graphs" (.-pathname (js/URL. (.-url request))))
           (not= "GET" (.-method request)))
      (http/error-response "method not allowed" 405)

      (= "/training/graphs" (.-pathname (js/URL. (.-url request))))
      (let [email (normalize-email (.get (.-searchParams (js/URL. (.-url request))) "email"))
            owner (normalize-email (aget env "TRAINING_OWNER_EMAIL"))]
        (if-not email
          (http/bad-request "invalid email")
          (p/let [rows (<rows env
                             (str "SELECT DISTINCT g.graph_id AS id, g.graph_name AS name FROM graphs g "
                                  "JOIN users u ON lower(u.email) = ? AND u.email_verified = 1 "
                                  "WHERE g.graph_ready_for_use = 1 "
                                  "AND (lower(u.email) = ? OR EXISTS (SELECT 1 FROM training_allowed_accounts a WHERE a.email = lower(u.email))) "
                                  "AND (g.user_id = u.id OR EXISTS (SELECT 1 FROM graph_members m WHERE m.graph_id = g.graph_id AND m.user_id = u.id)) "
                                  "ORDER BY lower(g.graph_name), g.graph_id") email owner)]
            (http/json-response nil {:graphs (mapv (fn [row] {:id (aget row "id")
                                                              :name (or (aget row "name") (aget row "id"))}) rows)}))))

      (= "GET" (.-method request))
      (p/let [rows (<rows env "SELECT email FROM training_allowed_accounts ORDER BY email")]
        (http/json-response nil {:owner (normalize-email (aget env "TRAINING_OWNER_EMAIL"))
                                 :emails (mapv #(aget % "email") rows)}))

      (contains? #{"PUT" "DELETE"} (.-method request))
      (p/let [text (.text request)]
        (if (> (count text) 512)
          (http/bad-request "request too large")
          (let [body (try (js/JSON.parse text) (catch :default _ nil))
                email (normalize-email (some-> body (aget "email")))
                owner (normalize-email (aget env "TRAINING_OWNER_EMAIL"))]
            (cond
              (nil? email) (http/bad-request "invalid email")
              (= email owner) (http/bad-request "owner account is permanently allowed")
              :else
              (p/do!
               (if (= "PUT" (.-method request))
                 (common/<d1-run (aget env "DB") "INSERT OR IGNORE INTO training_allowed_accounts (email) VALUES (?)" email)
                 (p/do!
                  (common/<d1-run (aget env "DB") "DELETE FROM training_allowed_accounts WHERE email = ?" email)
                  (<close-user-sockets! env email)))
               (http/json-response nil {:ok true}))))))

      :else (http/error-response "method not allowed" 405))))
