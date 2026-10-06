# Boskey - Bhavu Bot Instructions

## Start

From the Boskey directory:

    pm2 start boskey

## Check status

    pm2 status

## View logs

    pm2 logs boskey

## Restart

    pm2 restart boskey

AWS Secrets Manager is read once at startup. After changing the Bhavu secret, restart with pm2 restart boskey.

## Stop

    pm2 stop boskey

## Save PM2 process

    pm2 save

## Important

- Bot: Bhavu
- Symbol: ETHUSD
- Product ID: 3136
- AWS secret: Bhavu
- Manual orders are ignored.
- Only BHV- orders are managed.
- Do not delete bhavu_state.json while Bhavu has active orders.
