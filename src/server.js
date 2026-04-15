import express from 'express';
import cors from 'cors';
import dotenv from 'dotenv';
import crypto from 'crypto'; // Biblioteca nativa para a criptografia exigida

dotenv.config();

const app = express();
app.use(cors());
app.use(express.json());

// Constantes do .env
const SHOPEE_HOST = "https://partner.shopeemobile.com";
const PARTNER_ID = process.env.SHOPEE_PARTNER_ID;
const PARTNER_KEY = process.env.SHOPEE_PARTNER_KEY;
const SHOP_ID = process.env.SHOP_ID;
const ACCESS_TOKEN = process.env.ACCESS_TOKEN;

// 🛡️ O Leão de Chácara: Função que assina cada requisição
function generateShopeeAuth(apiPath) {
  const timestamp = Math.floor(Date.now() / 1000);
  const baseString = `${PARTNER_ID}${apiPath}${timestamp}${ACCESS_TOKEN}${SHOP_ID}`;
  const sign = crypto.createHmac('sha256', PARTNER_KEY).update(baseString).digest('hex');
  return { timestamp, sign };
}

// 📥 ROTA 1: Puxar a fila de avaliações
app.get('/api/reviews/fetch', async (req, res) => {
  try {
    const apiPath = "/api/v2/product/get_comment"; 
    const { timestamp, sign } = generateShopeeAuth(apiPath);

    const url = `${SHOPEE_HOST}${apiPath}?partner_id=${PARTNER_ID}&timestamp=${timestamp}&access_token=${ACCESS_TOKEN}&shop_id=${SHOP_ID}&sign=${sign}&page_size=20`;

    const response = await fetch(url);
    const data = await response.json();

    return res.status(200).json(data);
  } catch (error) {
    console.error("Erro ao puxar avaliações:", error);
    return res.status(500).json({ error: "Falha na conexão com a plataforma." });
  }
});

// 📤 ROTA 2: Enviar a resposta aprovada
app.post('/api/reviews/reply', async (req, res) => {
  try {
    const { comment_id, resposta_texto } = req.body;

    if (!comment_id || !resposta_texto) {
      return res.status(400).json({ error: "Faltam os dados do comentário." });
    }

    const apiPath = "/api/v2/product/reply_comment";
    const { timestamp, sign } = generateShopeeAuth(apiPath);

    const url = `${SHOPEE_HOST}${apiPath}?partner_id=${PARTNER_ID}&timestamp=${timestamp}&access_token=${ACCESS_TOKEN}&shop_id=${SHOP_ID}&sign=${sign}`;

    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        comment_list: [
          {
            comment_id: comment_id,
            comment: resposta_texto
          }
        ]
      })
    });

    const data = await response.json();
    return res.status(200).json(data);

  } catch (error) {
    console.error("Erro ao publicar resposta:", error);
    return res.status(500).json({ error: "Falha ao enviar resposta para a plataforma." });
  }
});

const PORT = process.env.PORT || 4000;
app.listen(PORT, () => {
  console.log(`🔌 Motor de Integração isolado rodando na porta ${PORT}`);
});
