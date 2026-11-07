# Proposition : adresse de livraison dans l'objet `commande`

Équipe Massar – agent Logistique / Livraison. Projet *Conduite de projet*, ESTIN, 2026-2027.

## Constat

L'exemple du registre `INTER_AGENT_DATA_CONTRACT` pour l'objet `commande` contient `numero_commande`, `date_commande`, `client {nom, email}`, `lignes`, `montant_total_ht` et `statut`. Il ne contient **aucune adresse de livraison**. Or l'agent Logistique ne peut pas planifier une tournée sans savoir où livrer.

## Proposition (champs facultatifs, rétrocompatibles)

Ajouter à `commande`, sans rien retirer ni renommer (les champs inconnus sont ignorés, règle commune du contrat) :

| Champ | Type | Exemple |
|-------|------|---------|
| `adresse_livraison` | texte, ou objet `{rue, code_postal, ville, wilaya, pays}` | `"12 rue des Frères, Béjaïa"` |
| `latitude`, `longitude` | nombres (degrés décimaux) | `36.7509`, `5.0567` |

L'adresse peut aussi se trouver dans `client.adresse`.

## Comportement actuel de notre agent

- Il lit, dans cet ordre : `adresse_livraison`, puis `client.adresse` ; les coordonnées dans l'adresse (objet) ou à la racine de la commande.
- Une commande confirmée **sans adresse ni coordonnées** n'est pas expédiée : l'agent lève une alerte `address_missing` et la rattrape dès qu'une nouvelle version de la commande contient l'adresse.
- Une adresse en texte sera convertie en coordonnées (géocodage) à l'itération 3.
- Si l'agent Commercial n'est pas souscrit, un employé saisit la commande depuis le tableau de bord de l'entreprise ; les mêmes champs s'appliquent.

## Questions ouvertes pour l'enseignant

1. Le type `commande` peut-il recevoir ces champs facultatifs (demande via une *issue* sur le dépôt du noyau) ?
2. Le `stock_level` publié par l'agent Stock est-il déjà diminué des commandes confirmées ? Notre agent compare la quantité commandée à `quantite_disponible` et ne réserve pas de stock lui-même.
3. Quel est l'`external_id` d'un `stock_level` (un par produit, ou un par produit et par entrepôt) ? Notre agent additionne les quantités de tous les objets ayant le même `sku`.
