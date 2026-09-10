from db import get_conn

conn = get_conn()
rows = conn.execute(
    "SELECT pm_id, label, stripe_payment_method_id, stripe_customer_id, is_default, created_at FROM payment_methods"
).fetchall()
conn.close()

print("Payment methods:")
for r in rows:
    print(dict(r))
