# Shopify + WhatsApp Order Management Flow

## Goal

Build a software where a customer interacts with your WhatsApp number, and your backend automatically updates their Shopify order.

Example:

```text
Customer places order

↓

Shopify Order Created

↓

Send WhatsApp Template

"Reply CONFIRM or CANCEL"

↓

Customer replies

CONFIRM

↓

Update Shopify Order

↓

Send Success Message
```

---

# Step 1: Generate Access Token

Your custom app uses **Client Credentials**.

## Request

```bash
curl --location 'https://thetavas.myshopify.com/admin/oauth/access_token' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--data-urlencode 'grant_type=client_credentials' \
--data-urlencode 'client_id=YOUR_CLIENT_ID' \
--data-urlencode 'client_secret=YOUR_CLIENT_SECRET'
```

## Response

```json
{
  "access_token":"shpat_xxxxxxxxx",
  "expires_in":86399
}
```

Store this token.

Every Shopify API requires

```http
X-Shopify-Access-Token: shpat_xxxxxxxxx
```

---

# Step 2: Fetch Order

Search by order number.

```bash
curl --location 'https://thetavas.myshopify.com/admin/api/2025-07/graphql.json' \
--header 'Content-Type: application/json' \
--header 'X-Shopify-Access-Token: ACCESS_TOKEN' \
--data '{
  "query":"query { orders(first:1, query:\"name:tavas3723\") { edges { node { id name email createdAt displayFinancialStatus displayFulfillmentStatus totalPriceSet { shopMoney { amount currencyCode } } } } } }"
}'
```

Returns

* GraphQL Order ID
* Order Number
* Email
* Amount
* Financial Status
* Fulfillment Status

Example

```text
gid://shopify/Order/12186377879920
```

This ID is required for every update.

---

# Step 3: Add Confirmed Tag

Instead of cancelling, better to mark as confirmed.

```bash
curl --location 'https://thetavas.myshopify.com/admin/api/2025-07/graphql.json' \
--header 'Content-Type: application/json' \
--header 'X-Shopify-Access-Token: ACCESS_TOKEN' \
--data '{
  "query":"mutation tagsAdd($id:ID!, $tags:[String!]!) { tagsAdd(id:$id,tags:$tags){ userErrors { message } } }",
  "variables":{
      "id":"gid://shopify/Order/12186377879920",
      "tags":["confirmed"]
  }
}'
```

---

# Step 4: Cancel Order

```bash
curl --location 'https://thetavas.myshopify.com/admin/api/2025-07/graphql.json' \
--header 'Content-Type: application/json' \
--header 'X-Shopify-Access-Token: ACCESS_TOKEN' \
--data '{
  "query":"mutation orderCancel($orderId:ID!, $reason:OrderCancelReason!, $restock:Boolean!){ orderCancel(orderId:$orderId, reason:$reason, restock:$restock){ job { id } userErrors { message } } }",
  "variables":{
      "orderId":"gid://shopify/Order/12186377879920",
      "reason":"CUSTOMER",
      "restock":true
  }
}'
```

**Important**

Cancelled orders cannot be restored.

---

# Step 5: Update Shipping Address

```bash
curl --location 'https://thetavas.myshopify.com/admin/api/2025-07/graphql.json' \
--header 'Content-Type: application/json' \
--header 'X-Shopify-Access-Token: ACCESS_TOKEN' \
--data '{
  "query":"mutation orderUpdate($input:OrderInput!){ orderUpdate(input:$input){ order{ id } userErrors{ message } } }",
  "variables":{
      "input":{
          "id":"gid://shopify/Order/12186377879920",
          "shippingAddress":{
              "firstName":"John",
              "lastName":"Doe",
              "address1":"123 Street",
              "city":"Mumbai",
              "province":"Maharashtra",
              "zip":"400001",
              "country":"India",
              "phone":"9876543210"
          }
      }
  }
}'
```

---

# Step 6: Search Customer

Requires

```
read_customers
```

permission.

```graphql
query {
  customers(first:1, query:"phone:+919876543210"){
    edges{
      node{
        id
        firstName
        phone
        email
      }
    }
  }
}
```

---

# Important Finding

You **cannot** search Orders by

```
shippingAddress.phone
```

Shopify ignores it.

This means this does **NOT** work:

```
phone:9876543210
```

for orders.

---

# Recommended Architecture

Instead of searching Shopify every time, maintain your own mapping.

```
Shopify Order Created

↓

Order ID
Customer Phone

↓

Store in Database

+919876543210

↓

gid://shopify/Order/12186377879920
```

Then WhatsApp replies become very easy.

---

# WhatsApp Flow

```
Customer places order

↓

Shopify Webhook

↓

Receive Order

↓

Extract

Order ID
Phone Number
Customer Name

↓

Store Mapping

Phone
↓

Order ID

↓

Send WhatsApp Template

------------------------------------

Hi John,

Your order is received.

Reply

CONFIRM
CANCEL
CHANGE ADDRESS

------------------------------------
```

---

# Customer replies

```
CONFIRM
```

Backend

```
Phone Number

↓

Database Lookup

↓

Order ID

↓

Shopify API

↓

Add Tag

confirmed

↓

Send WhatsApp

Order Confirmed
```

---

```
CANCEL
```

Backend

```
Phone

↓

Database

↓

Order ID

↓

Shopify orderCancel

↓

WhatsApp

Order Cancelled
```

---

```
CHANGE ADDRESS
```

Backend

```
Phone

↓

Database

↓

Order ID

↓

Ask for new address

↓

Call orderUpdate

↓

Address Updated

↓

Send Confirmation
```

---

# Permissions Required

Your app should have at least:

```
read_orders
write_orders
read_customers   (if searching customers)
write_customers  (optional)
```

---

# Suggested Software Architecture

```
Customer
      │
      ▼
WhatsApp Cloud API
      │
      ▼
Webhook (Your Backend)
      │
      ├── Parse Message
      ├── Identify Customer (Phone)
      ├── Find Order ID (Database)
      ├── Call Shopify API
      ├── Update Order
      └── Send WhatsApp Reply
      │
      ▼
Shopify Admin GraphQL API
```

This architecture avoids unsupported order-by-phone searches, minimizes Shopify API calls, and is well-suited for building a production WhatsApp + Shopify order management system.
