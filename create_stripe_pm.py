import os
import stripe
from dotenv import load_dotenv

load_dotenv()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# 1) Create a PaymentMethod using a Stripe test card
pm = stripe.PaymentMethod.create(
    type="card",
    card={"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"},
)
print("PaymentMethod:", pm.id)

# 2) Create a Customer and attach PM
customer = stripe.Customer.create()
stripe.PaymentMethod.attach(pm.id, customer=customer.id)

# 3) Set as default for invoice/payments (helpful)
stripe.Customer.modify(
    customer.id,
    invoice_settings={"default_payment_method": pm.id},
)

print("Customer:", customer.id)
print("Default PM:", pm.id)
