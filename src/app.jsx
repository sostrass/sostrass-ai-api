import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';

// URLs dos seus dois backends no Railway (ou Local)
const MOTOR_API = "http://localhost:4000/api"; // Motor Shopee
const CEREBRO_API = "http://localhost:3000/api"; // Cérebro IA

export default function ReputacaoHub() {
  const [reviews, setReviews] = useState([]);
  const [loading, setLoading] = useState(true);

  // 1. Puxa as avaliações REAIS da Shopee ao abrir o app
  useEffect(() => {
    fetch(`${MOTOR_API}/reviews/fetch`)
      .then(res => res.json())
      .then(data => {
        setReviews(data.response.comment_list);
        setLoading(false);
      });
  }, []);

  return (
    <div className="min-h-screen bg-black bg-gradient-to-b from-gray-900 to-black text-white p-6 font-sans">
      {/* Header Estilo iOS */}
      <header className="max-w-md mx-auto mb-8 pt-4">
        <h1 className="text-3xl font-bold tracking-tight">Avaliações</h1>
        <p className="text-gray-400 text-sm">Sóstrass Reputação Hub</p>
      </header>

      <main className="max-w-md mx-auto space-y-4">
        <AnimatePresence>
          {loading ? (
            <SkeletonLoader />
          ) : (
            reviews.map((rev) => (
              <ReviewCard key={rev.comment_id} review={rev} />
            ))
          )}
        </AnimatePresence>
      </main>

      {/* TABS iOS no Rodapé */}
      <nav className="fixed bottom-6 left-1/2 -translate-x-1/2 w-[90%] max-w-md bg-white/10 backdrop-blur-xl border border-white/20 rounded-3xl p-4 flex justify-around items-center shadow-2xl">
        <button className="text-blue-400 font-semibold">Reviews</button>
        <button className="text-gray-400">Autopilot</button>
        <button className="text-gray-400">Ajustes</button>
      </nav>
    </div>
  );
}

// Componente do Card com a lógica de Gerar IA e Enviar Shopee
function ReviewCard({ review }) {
  const [respostaIA, setRespostaIA] = useState("");
  const [gerando, setGerando] = useState(false);

  const handleGerar = async () => {
    setGerando(true);
    const res = await fetch(`${CEREBRO_API}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        produto: review.item_name,
        estrelas: review.rating_star,
        comentario: review.comment
      })
    });
    const data = await res.json();
    setRespostaIA(data.resposta_generated);
    setGerando(false);
  };

  return (
    <motion.div 
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-white/5 backdrop-blur-lg border border-white/10 p-5 rounded-2xl shadow-xl"
    >
      <div className="flex justify-between items-start mb-3">
        <span className="text-yellow-400 font-bold">{"⭐".repeat(review.rating_star)}</span>
        <span className="text-[10px] text-gray-500 uppercase">Shopee</span>
      </div>
      
      <p className="text-sm font-medium mb-1">{review.item_name}</p>
      <p className="text-xs text-gray-400 mb-4">"{review.comment}"</p>

      {!respostaIA ? (
        <button 
          onClick={handleGerar}
          className="w-full bg-white text-black font-bold py-3 rounded-xl active:scale-95 transition-all"
        >
          {gerando ? "IA pensando..." : "Gerar Resposta"}
        </button>
      ) : (
        <div className="space-y-3">
          <textarea 
            className="w-full bg-black/40 border border-white/10 rounded-lg p-3 text-xs text-gray-300 h-24"
            value={respostaIA}
            onChange={(e) => setRespostaIA(e.target.value)}
          />
          <button className="w-full bg-blue-600 text-white font-bold py-3 rounded-xl shadow-lg shadow-blue-900/20">
            Aprovar e Enviar para Shopee
          </button>
        </div>
      )}
    </motion.div>
  );
}
